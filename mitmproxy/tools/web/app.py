import hashlib
import json
import logging
import os.path
import re
from itertools import chain
from io import BytesIO
import asyncio
import pprint

import mitmproxy.flow
import tornado.escape
import tornado.web
import tornado.websocket
from mitmproxy import contentviews
from mitmproxy import exceptions
from mitmproxy import flowfilter
from mitmproxy import http
from mitmproxy import io
from mitmproxy import log
from mitmproxy import version
from mitmproxy import optmanager
import mitmproxy.tools.web.master # noqa


def flow_to_json(flow: mitmproxy.flow.Flow) -> dict:
    """
    Remove flow message content and cert to save transmission space.

    Args:
        flow: The original flow.
    """
    f = {
        "id": flow.id,
        "intercepted": flow.intercepted,
        "client_conn": flow.client_conn.get_state(),
        "server_conn": flow.server_conn.get_state(),
        "type": flow.type,
        "modified": flow.modified(),
        "marked": flow.marked,
    }
    # .alpn_proto_negotiated is bytes, we need to decode that.
    for conn in "client_conn", "server_conn":
        if f[conn]["alpn_proto_negotiated"] is None:
            continue
        f[conn]["alpn_proto_negotiated"] = \
            f[conn]["alpn_proto_negotiated"].decode(errors="backslashreplace")
    # There are some bytes in here as well, let's skip it until we have them in the UI.
    f["client_conn"].pop("tls_extensions", None)
    if flow.error:
        f["error"] = flow.error.get_state()

    if isinstance(flow, http.HTTPFlow):
        if flow.request:
            if flow.request.raw_content:
                content_length = len(flow.request.raw_content)
                content_hash = hashlib.sha256(flow.request.raw_content).hexdigest()
            else:
                content_length = None
                content_hash = None
            f["request"] = {
                "method": flow.request.method,
                "scheme": flow.request.scheme,
                "host": flow.request.host,
                "port": flow.request.port,
                "path": flow.request.path,
                "http_version": flow.request.http_version,
                "headers": tuple(flow.request.headers.items(True)),
                "contentLength": content_length,
                "contentHash": content_hash,
                "timestamp_start": flow.request.timestamp_start,
                "timestamp_end": flow.request.timestamp_end,
                "is_replay": flow.request.is_replay,
                "pretty_host": flow.request.pretty_host,
            }
        if flow.response:
            if flow.response.raw_content:
                content_length = len(flow.response.raw_content)
                content_hash = hashlib.sha256(flow.response.raw_content).hexdigest()
            else:
                content_length = None
                content_hash = None
            f["response"] = {
                "http_version": flow.response.http_version,
                "status_code": flow.response.status_code,
                "reason": flow.response.reason,
                "headers": tuple(flow.response.headers.items(True)),
                "contentLength": content_length,
                "contentHash": content_hash,
                "timestamp_start": flow.response.timestamp_start,
                "timestamp_end": flow.response.timestamp_end,
                "is_replay": flow.response.is_replay,
            }
    f.get("server_conn", {}).pop("cert", None)
    f.get("client_conn", {}).pop("mitmcert", None)

    return f


def logentry_to_json(e: log.LogEntry) -> dict:
    return {
        "id": id(e),  # we just need some kind of id.
        "message": e.msg,
        "level": e.level
    }


class APIError(tornado.web.HTTPError):
    pass


class RequestHandler(tornado.web.RequestHandler):
    def write(self, chunk):
        # Writing arrays on the top level is ok nowadays.
        # http://flask.pocoo.org/docs/0.11/security/#json-security
        if isinstance(chunk, list):
            chunk = tornado.escape.json_encode(chunk)
            self.set_header("Content-Type", "application/json; charset=UTF-8")
        super(RequestHandler, self).write(chunk)

    def set_default_headers(self):
        super().set_default_headers()
        self.set_header("Server", version.MITMPROXY)
        self.set_header("X-Frame-Options", "DENY")
        self.add_header("X-XSS-Protection", "1; mode=block")
        self.add_header("X-Content-Type-Options", "nosniff")
        self.add_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "connect-src 'self' ws:; "
            "style-src   'self' 'unsafe-inline'"
        )

    @property
    def json(self):
        if not self.request.headers.get("Content-Type", "").startswith("application/json"):
            raise APIError(400, "Invalid Content-Type, expected application/json.")
        try:
            return json.loads(self.request.body.decode())
        except Exception as e:
            raise APIError(400, "Malformed JSON: {}".format(str(e)))

    @property
    def filecontents(self):
        """
        Accept either a multipart/form file upload or just take the plain request body.

        """
        if self.request.files:
            return next(iter(self.request.files.values()))[0].body
        else:
            return self.request.body

    @property
    def view(self) -> "mitmproxy.addons.view.View":
        return self.application.master.view

    @property
    def master(self) -> "mitmproxy.tools.web.master.WebMaster":
        return self.application.master

    @property
    def scenarios(self):
        return self.application.master.view.scenarios

    @property
    def scenario(self):
        return self.application.master.view.scenario

    @property
    def mock(self):
        return self.application.master.view.mock

    @property
    def learning(self):
        return self.application.master.view.learning

    @property
    def flow(self) -> mitmproxy.flow.Flow:
        flow_id = str(self.path_kwargs["flow_id"])

        # FIXME: Add a facility to addon.view to safely access the store
        flow = self.view.get_by_id(flow_id)
        if flow:
            return flow
        else:
            raise APIError(404, "Flow not found.")

    def write_error(self, status_code: int, **kwargs):
        if "exc_info" in kwargs and isinstance(kwargs["exc_info"][1], APIError):
            self.finish(kwargs["exc_info"][1].log_message)
        else:
            super().write_error(status_code, **kwargs)


class IndexHandler(RequestHandler):
    def get(self):
        token = self.xsrf_token  # https://github.com/tornadoweb/tornado/issues/645
        assert token
        self.render("index.html")


class FilterHelp(RequestHandler):
    def get(self):
        self.write(dict(
            commands=flowfilter.help
        ))


class WebSocketEventBroadcaster(tornado.websocket.WebSocketHandler):
    # raise an error if inherited class doesn't specify its own instance.
    connections: set = None

    def open(self):
        self.connections.add(self)

    def on_close(self):
        self.connections.remove(self)

    @classmethod
    def broadcast(cls, **kwargs):
        message = json.dumps(kwargs, ensure_ascii=False).encode("utf8", "surrogateescape")

        for conn in cls.connections:
            try:
                conn.write_message(message)
            except Exception:  # pragma: no cover
                logging.error("Error sending message", exc_info=True)


class ClientConnection(WebSocketEventBroadcaster):
    connections: set = set()

class Scenario(RequestHandler):
    def post(self, scenario):
        if not scenario in self.scenarios:
            self.view.addScen(scenario)
        self.view.setScen(scenario)

class DeleteScenario(RequestHandler):
    def post(self, scenario):
        keys = list(self.scenarios.keys())
        index = keys.index(scenario)
        if(len(keys) == 1):
            self.master.view.setScen("")
        else:
            self.master.view.setScen(keys[index-1]) if index else self.master.view.setScen(keys[index+1])

        self.view.remove(self.scenarios[scenario][0])
        self.scenarios.pop(scenario)

class CopyScenario(RequestHandler):
    def post(self, scenario):
        self.scenarios[scenario] = self.scenarios[self.scenario]
        del self.scenarios[self.scenario]
        self.master.view.setScen(scenario)

class SwitchBouchonMode(RequestHandler):
    def post(self):
        self.master.view.switchMock()

class SwitchLearning(RequestHandler):
    def post(self):
        self.master.view.switchLearning()

class RuleHandler(RequestHandler):
    def delete(self, flow_id):
        if self.flow.killable:
            self.flow.kill()
        self.view.remove([self.flow])

    def put(self, flow_id):
        tmpdict = self.json.copy()
        index = tmpdict.pop('Index')
        label = tmpdict.pop('Label')
        if index == -1:
            self.scenarios[self.scenario][1][flow_id].addRule(label,tmpdict)
        else:
            self.scenarios[self.scenario][1][flow_id].setRule(label,tmpdict,index)

class RuleUp(RequestHandler):
    def post(self, flow_id, label, index):
        self.scenarios[self.scenario][1][flow_id].switchRules(label,int(index),int(index)-1)

class RuleDown(RequestHandler):
    def post(self, flow_id, label, index):
        self.scenarios[self.scenario][1][flow_id].switchRules(label,int(index),int(index)+1)

class RuleDelete(RequestHandler):
    def post(self, flow_id, label, index):
        self.scenarios[self.scenario][1][flow_id].deleteRule(label,int(index))

class Flows(RequestHandler):
    def get(self):
        dic = {}
        li = []
        for f in self.view:
            dic = flow_to_json(f)
            for i in self.scenarios.keys():
                if f in self.scenarios[i][0]:
                    dic["scenario"] = i
            r = {'Headers': [], 'Content': [], 'URI': []}
            if "scenario" in dic:
                r = self.scenarios[dic["scenario"]][1][f.id].toDict()
            li.append((dic,r))
        if self.scenario != "":
            li.append(self.scenario)

        if self.view.mock:
            self.view.switchMock()
        if self.view.learning:
            self.view.switchLearning()
        self.write(li)
        #self.write([flow_to_json(f) for f in self.view])


class DumpFlows(RequestHandler):
    def get(self):
        self.set_header("Content-Disposition", "attachment; filename=" + self.scenario + ".fl")
        self.set_header("Content-Type", "application/octet-stream")

        bio = BytesIO()
        fw = io.FlowWriter(bio)
        bio.write(bytes("{",'UTF-8'))
        for id, m in self.scenarios[self.scenario][1].items():
            bio.write(bytes("'" + id + "'" + ": " + str(m) + ", ",'UTF-8'))
        bio.write(bytes("} \n",'UTF-8'))
        for f in self.scenarios[self.scenario][0]:
            fw.add(f)
        self.write(bio.getvalue())
        bio.close()

    def post(self):
        if(not self.learning):
            self.master.view.switchLearning()
        if self.scenario:
            self.view.remove(self.scenarios[self.scenario][0])
        self.scenarios[self.scenario] = ([],{})
        bio = BytesIO(self.filecontents)
        matchers = eval(bio.readline().decode())
        for id, matcher in matchers.items():
            self.view.newMatcher(id, matcher)
        for i in io.FlowReader(bio).stream():
            asyncio.call_soon(self.master.load_flow, i)

        bio.close()


class ClearAll(RequestHandler):
    def post(self):
        self.view.clear()
        self.master.events.clear()


class ResumeFlows(RequestHandler):
    def post(self):
        for f in self.view:
            f.resume()
            self.view.update([f])


class KillFlows(RequestHandler):
    def post(self):
        for f in self.view:
            if f.killable:
                f.kill()
                self.view.update([f])


class ResumeFlow(RequestHandler):
    def post(self, flow_id):
        self.flow.resume()
        self.view.update([self.flow])


class KillFlow(RequestHandler):
    def post(self, flow_id):
        if self.flow.killable:
            self.flow.kill()
            self.view.update([self.flow])


class FlowHandler(RequestHandler):
    def delete(self, flow_id):
        if self.flow.killable:
            self.flow.kill()
        self.scenarios[self.scenario][0].remove(self.flow)
        self.scenarios[self.scenario][1].pop(self.flow.id,None)
        self.view.remove([self.flow])



    def put(self, flow_id):
        flow = self.flow
        flow.backup()
        try:
            for a, b in self.json.items():
                if a == "request" and hasattr(flow, "request"):
                    request = flow.request
                    for k, v in b.items():
                        if k in ["method", "scheme", "host", "path", "http_version"]:
                            setattr(request, k, str(v))
                        elif k == "port":
                            request.port = int(v)
                        elif k == "headers":
                            request.headers.clear()
                            for header in v:
                                request.headers.add(*header)
                        elif k == "content":
                            request.text = v
                        else:
                            raise APIError(400, "Unknown update request.{}: {}".format(k, v))

                elif a == "response" and hasattr(flow, "response"):
                    response = flow.response
                    for k, v in b.items():
                        if k in ["msg", "http_version"]:
                            setattr(response, k, str(v))
                        elif k == "code":
                            response.status_code = int(v)
                        elif k == "headers":
                            response.headers.clear()
                            for header in v:
                                response.headers.add(*header)
                        elif k == "content":
                            response.text = v
                        else:
                            raise APIError(400, "Unknown update response.{}: {}".format(k, v))
                else:
                    raise APIError(400, "Unknown update {}: {}".format(a, b))
        except APIError:
            flow.revert()
            raise
        self.view.update([flow])


class DuplicateFlow(RequestHandler):
    def post(self, flow_id):
        f = self.flow.copy()
        self.view.add([f])
        self.view.addFlow(f)
        self.write(f.id)


class RevertFlow(RequestHandler):
    def post(self, flow_id):
        if self.flow.modified():
            self.flow.revert()
            self.view.update([self.flow])


class ReplayFlow(RequestHandler):
    def post(self, flow_id):
        self.flow.backup()
        self.flow.response = None
        self.view.update([self.flow])

        try:
            self.master.replay_request(self.flow)
        except exceptions.ReplayException as e:
            raise APIError(400, str(e))


class FlowContent(RequestHandler):
    def post(self, flow_id, message):
        self.flow.backup()
        message = getattr(self.flow, message)
        message.content = self.filecontents
        self.view.update([self.flow])

    def get(self, flow_id, message):
        message = getattr(self.flow, message)

        if not message.raw_content:
            raise APIError(400, "No content.")

        content_encoding = message.headers.get("Content-Encoding", None)
        if content_encoding:
            content_encoding = re.sub(r"[^\w]", "", content_encoding)
            self.set_header("Content-Encoding", content_encoding)

        original_cd = message.headers.get("Content-Disposition", None)
        filename = None
        if original_cd:
            filename = re.search('filename=([-\w" .()]+)', original_cd)
            if filename:
                filename = filename.group(1)
        if not filename:
            filename = self.flow.request.path.split("?")[0].split("/")[-1]

        filename = re.sub(r'[^-\w" .()]', "", filename)
        cd = "attachment; filename={}".format(filename)
        self.set_header("Content-Disposition", cd)
        self.set_header("Content-Type", "application/text")
        self.set_header("X-Content-Type-Options", "nosniff")
        self.set_header("X-Frame-Options", "DENY")
        self.write(message.raw_content)


class FlowContentView(RequestHandler):
    def get(self, flow_id, message, content_view):
        message = getattr(self.flow, message)

        description, lines, error = contentviews.get_message_content_view(
            content_view.replace('_', ' '), message
        )
        #        if error:
        #           add event log

        self.write(dict(
            lines=list(lines),
            description=description
        ))


class Events(RequestHandler):
    def get(self):
        self.write([logentry_to_json(e) for e in self.master.events.data])


class Settings(RequestHandler):
    def get(self):
        self.write(dict(
            version=version.VERSION,
            mode=str(self.master.options.mode),
            intercept_active=self.master.options.intercept_active,
            intercept=self.master.options.intercept,
            showhost=self.master.options.showhost,
            upstream_cert=self.master.options.upstream_cert,
            rawtcp=self.master.options.rawtcp,
            http2=self.master.options.http2,
            websocket=self.master.options.websocket,
            anticache=self.master.options.anticache,
            anticomp=self.master.options.anticomp,
            stickyauth=self.master.options.stickyauth,
            stickycookie=self.master.options.stickycookie,
            stream=self.master.options.stream_large_bodies,
            contentViews=[v.name.replace(' ', '_') for v in contentviews.views],
            listen_host=self.master.options.listen_host,
            listen_port=self.master.options.listen_port,
            server=self.master.options.server,
        ))

    def put(self):
        update = self.json
        option_whitelist = {
            "intercept", "showhost", "upstream_cert",
            "rawtcp", "http2", "websocket", "anticache", "anticomp",
            "stickycookie", "stickyauth", "stream_large_bodies"
        }
        for k in update:
            if k not in option_whitelist:
                raise APIError(400, "Unknown setting {}".format(k))
        self.master.options.update(**update)


class Options(RequestHandler):
    def get(self):
        self.write(optmanager.dump_dicts(self.master.options))

    def put(self):
        update = self.json
        try:
            self.master.options.update(**update)
        except Exception as err:
            raise APIError(400, "{}".format(err))


class SaveOptions(RequestHandler):
    def post(self):
        # try:
        #     optmanager.save(self.master.options, CONFIG_PATH, True)
        # except Exception as err:
        #     raise APIError(400, "{}".format(err))
        pass


class Application(tornado.web.Application):
    def __init__(self, master, debug):
        self.master = master
        handlers = [
            (r"/", IndexHandler),
            (r"/filter-help(?:\.json)?", FilterHelp),
            (r"/updates", ClientConnection),
            (r"/events(?:\.json)?", Events),
            (r"/flows(?:\.json)?", Flows),
            (r"/flows/dump", DumpFlows),
            (r"/flows/resume", ResumeFlows),
            (r"/flows/kill", KillFlows),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)", FlowHandler),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/resume", ResumeFlow),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/kill", KillFlow),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/duplicate", DuplicateFlow),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/replay", ReplayFlow),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/revert", RevertFlow),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/(?P<message>request|response)/content.data", FlowContent),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/(?P<message>request|response)/content/(?P<content_view>[0-9a-zA-Z\-\_]+)(?:\.json)?"
             ,FlowContentView),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/rule/update", RuleHandler),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/rule/(?P<label>[A-Z][a-z]+)/(?P<index>[0-9]+)/up", RuleUp),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/rule/(?P<label>[A-Z][a-z]+)/(?P<index>[0-9]+)/down", RuleDown),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/rule/(?P<label>[A-Z][a-z]+)/(?P<index>[0-9]+)/delete", RuleDelete),
            (r"/settings(?:\.json)?", Settings),
            (r"/clear", ClearAll),
            (r"/options(?:\.json)?", Options),
            (r"/options/save", SaveOptions),
            (r"/scenario/(?P<scenario>[^\/]*)", Scenario),
            (r"/scenario/(?P<scenario>[^\/]*)/remove", DeleteScenario),
            (r"/scenario/(?P<scenario>[^\/]*)/copy", CopyScenario),
            (r"/bouchon", SwitchBouchonMode),
            (r"/learn", SwitchLearning)
        ]
        settings = dict(
            template_path=os.path.join(os.path.dirname(__file__), "templates"),
            static_path=os.path.join(os.path.dirname(__file__), "static"),
            xsrf_cookies=True,
            cookie_secret=os.urandom(256),
            debug=debug,
            autoreload=False,
        )
        super().__init__(handlers, **settings)

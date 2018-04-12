class WebAddon:
    def load(self, loader):
        loader.add_option(
            "web_open_browser", bool, False,
            "Start a browser."
        )
        loader.add_option(
            "web_debug", bool, False,
            "Enable mitmweb debugging."
        )
        loader.add_option(
            "web_port", int, 3121,
            "Web UI port."
        )
        loader.add_option(
            "web_iface", str, "127.0.0.1",
            "Web UI interface."
        )

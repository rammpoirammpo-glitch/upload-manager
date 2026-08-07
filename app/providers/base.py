class BaseProvider:
    type = ""
    display_name = ""
    field_schema = []

    def __init__(self, config):
        self.config = config or {}

    def test(self):
        raise NotImplementedError

    def upload(self, local_path, remote_path, progress_cb):
        raise NotImplementedError

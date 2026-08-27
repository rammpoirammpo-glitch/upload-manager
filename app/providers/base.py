class BaseProvider:
    """Interface every provider implements.

    ``upload(local_path, remote_path, progress_cb, resume_state=None)``:
    - ``progress_cb(fraction)`` is called with a value in [0, 1].
    - ``resume_state`` is an opaque dict returned by a previous partial run
      of the SAME provider (e.g. an S3 multipart upload id); providers that
      support resumable uploads use it to continue instead of restarting.
      Providers that cannot resume simply ignore it.
    - Returns ``None`` on full success. Providers with partial progress may
      raise a provider-specific error that carries updated ``resume_state``
      (see S3Provider.S3MultipartError), which the queue persists so the
      next attempt resumes from the last uploaded part.
    """

    type = ""
    display_name = ""
    field_schema = []

    def __init__(self, config):
        self.config = config or {}

    def test(self):
        raise NotImplementedError

    def upload(self, local_path, remote_path, progress_cb, resume_state=None):
        raise NotImplementedError

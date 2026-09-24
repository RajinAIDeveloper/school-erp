"""
A ceiling on the size of any upload.

Django reads a request's files before a view can look at them: into memory, then into a
temporary file on disk. Without a ceiling a huge upload would fill the disk before any view
could refuse it. This handler stops storing files once a request is larger than
MAX_UPLOAD_REQUEST. The rest of the body is read and thrown away, so the view still answers
and can say the upload was too large. A proxy in front should refuse such a body sooner.
"""

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class CappedUploadHandler(FileUploadHandler):
    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        self.too_large = content_length > settings.MAX_UPLOAD_REQUEST

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        if self.too_large:
            raise StopUpload(connection_reset=False)

    def receive_data_chunk(self, raw_data, start):
        return raw_data

    def file_complete(self, file_size):
        return None

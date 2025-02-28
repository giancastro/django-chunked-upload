import hashlib
import uuid

from django.db import models
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from .settings import EXPIRATION_DELTA, UPLOAD_TO, STORAGE, DEFAULT_MODEL_USER_FIELD_NULL, DEFAULT_MODEL_USER_FIELD_BLANK
from .constants import CHUNKED_UPLOAD_CHOICES, UPLOADING


def generate_upload_id():
    return uuid.uuid4().hex


class AbstractChunkedUpload(models.Model):
    """
    Base chunked upload model. This model is abstract (doesn't create a table
    in the database).
    Inherit from this model to implement your own.
    """

    upload_id = models.CharField(max_length=32, unique=True, editable=False,
                                 default=generate_upload_id)
    file = models.FileField(max_length=255, upload_to=UPLOAD_TO,
                            storage=STORAGE)
    filename = models.CharField(max_length=255)
    offset = models.BigIntegerField(default=0)
    created_on = models.DateTimeField(auto_now_add=True)
    status = models.PositiveSmallIntegerField(choices=CHUNKED_UPLOAD_CHOICES,
                                              default=UPLOADING)
    completed_on = models.DateTimeField(null=True, blank=True)

    @property
    def expires_on(self):
        return self.created_on + EXPIRATION_DELTA

    @property
    def expired(self):
        return self.expires_on <= timezone.now()

    @property
    def md5(self):
        if getattr(self, '_md5', None) is None:
            md5 = hashlib.md5()
            for chunk in self.file.chunks():
                md5.update(chunk)
            self._md5 = md5.hexdigest()
        return self._md5

    def delete(self, delete_file=True, *args, **kwargs):
        if self.file:
            storage, path = self.file.storage, self.file.path
        super(AbstractChunkedUpload, self).delete(*args, **kwargs)
        if self.file and delete_file:
            storage.delete(path)

    def __str__(self):
        return u'<%s - upload_id: %s - bytes: %s - status: %s>' % (
            self.filename, self.upload_id, self.offset, self.status)

    def _get_blob_service_client(self):
        from azure.core.credentials import AzureNamedKeyCredential
        from azure.storage.blob import BlobServiceClient

        account_name = settings.AZURE_ACCOUNT_NAME
        account_key = settings.AZURE_ACCOUNT_KEY
        credential = AzureNamedKeyCredential(account_name, account_key)
        account_url = f"https://{account_name}.blob.core.windows.net"
        return BlobServiceClient(account_url=account_url, credential=credential)

    def append_chunk(self, chunk, chunk_size=None, save=True):
        if getattr(settings, 'USE_AZURE_APPEND_BLOB', False):
            container_name = settings.AZURE_MEDIA_CONTAINER
            blob_name = self.file.name

            blob_service_client = self._get_blob_service_client()
            container_client = blob_service_client.get_container_client(container_name)
            append_blob_client = container_client.get_blob_client(blob_name)

            # For new uploads, ensure we have a clean slate:
            if self.offset == 0:
                if append_blob_client.exists():
                    # Delete existing blob if it's not of the Append Blob type
                    append_blob_client.delete_blob()
                append_blob_client.create_append_blob()

            # Append the chunk data
            data = chunk.read()
            append_blob_client.append_block(data)

            # Update offset accordingly
            if chunk_size is not None:
                self.offset += chunk_size
            elif hasattr(chunk, 'size'):
                self.offset += chunk.size
            else:
                self.offset += len(data)
        else:
            # Fallback for development using local storage
            self.file.close()
            with open(self.file.path, mode='ab') as file_obj:
                file_obj.write(chunk.read())
            if chunk_size is not None:
                self.offset += chunk_size
            elif hasattr(chunk, 'size'):
                self.offset += chunk.size
            else:
                self.offset = self.file.size

        self._md5 = None  # Clear cached MD5
        if save:
            self.save()
        if not getattr(settings, 'USE_AZURE_APPEND_BLOB', False):
            self.file.close()

    def get_uploaded_file(self):
        self.file.close()
        self.file.open(mode='rb')  # mode = read+binary
        return UploadedFile(file=self.file, name=self.filename,
                            size=self.offset)

    class Meta:
        abstract = True


class ChunkedUpload(AbstractChunkedUpload):
    """
    Default chunked upload model.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='chunked_uploads',
        null=DEFAULT_MODEL_USER_FIELD_NULL,
        blank=DEFAULT_MODEL_USER_FIELD_BLANK
    )

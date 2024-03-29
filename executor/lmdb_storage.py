import os
from typing import Dict,Optional
from warnings import warn
import lmdb
from docarray import Document, DocumentArray
from jina import Executor, requests
from jina.logging.logger import JinaLogger

from .commons import export_dump_streaming, import_metas


class _LMDBHandler:
    def __init__(self, file, map_size):
        # see https://lmdb.readthedocs.io/en/release/#environment-class for usage
        self.file = file
        self.map_size = map_size

    @property
    def env(self):
        return self._env

    def __enter__(self):
        self._env = lmdb.Environment(
            self.file,
            map_size=self.map_size,
            subdir=False,
            readonly=False,
            metasync=True,
            sync=True,
            map_async=False,
            mode=493,
            create=True,
            readahead=True,
            writemap=False,
            meminit=True,
            max_readers=126,
            max_dbs=0,  # means only one db
            max_spare_txns=1,
            lock=True,
        )
        return self._env

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self, '_env'):
            self._env.close()


class LMDBStorage(Executor):
    """An lmdb-based Storage Indexer for Jina

    For more information on lmdb check their documentation: https://lmdb.readthedocs.io/en/release/
    """

    def __init__(
        self,
        map_size: int = 1048576000,  # in bytes, 1000 MB
        default_access_paths: str = '@r',
        default_traversal_paths: Optional[str] = None,
        dump_path: str = None,
        default_return_embeddings: bool = True,
        *args,
        **kwargs,
    ):
        """
        :param map_size: the maximal size of teh database. Check more information at
            https://lmdb.readthedocs.io/en/release/#environment-class
        :param default_access_paths: fallback traversal path in case there is not traversal path sent in the request
        :param default_traversal_paths: please use default_access_paths
        :param default_return_embeddings: whether to return embeddings on search or not
        """
        super().__init__(*args, **kwargs)
        self.map_size = map_size
        if default_traversal_paths is not None:
            self.default_access_paths = default_traversal_paths
            warn("'default_traversal_paths' will be deprecated in the future, please use 'default_access_paths'.",
                 DeprecationWarning,
                 stacklevel=2)
        else:
            self.default_access_paths = default_access_paths

        self.file = os.path.join(self.workspace, 'db.lmdb')
        if not os.path.exists(self.workspace):
            os.makedirs(self.workspace)
        self.logger = JinaLogger(self.metas.name)

        result = kwargs.get('runtime_args', dict())
        if result is None:
            result = {}
        self.dump_path = dump_path or result.get(
            'dump_path', None
        )
        if self.dump_path is not None:
            self.logger.info(f'Importing data from {self.dump_path}')
            ids, metas = import_metas(self.dump_path, str(self.runtime_args.pea_id))
            da = DocumentArray()
            for id, meta in zip(ids, metas):
                serialized_doc = Document(meta)
                serialized_doc.id = id
                da.append(serialized_doc)
            self.index(da, parameters={'access_paths': '@r'})
        self.default_return_embeddings = default_return_embeddings

    def _handler(self):
        # required to create a new connection to the same file
        # on each subprocess
        # https://github.com/jnwatson/py-lmdb/issues/289
        return _LMDBHandler(self.file, self.map_size)

    @requests(on='/index')
    def index(self, docs: DocumentArray, parameters: Dict, **kwargs):
        """Add entries to the index

        :param docs: the documents to add
        :param parameters: parameters to the request
        """
        access_paths = parameters.get(
            'access_paths', self.default_access_paths
        )
        if docs is None:
            return
        with self._handler() as env:
            with env.begin(write=True) as transaction:
                for d in docs[access_paths]:
                    transaction.put(d.id.encode(), d.to_bytes())

    @requests(on='/update')
    def update(self, docs: DocumentArray, parameters: Dict, **kwargs):
        """Update entries from the index by id

        :param docs: the documents to update
        :param parameters: parameters to the request
        """
        access_paths = parameters.get(
            'access_paths', self.default_access_paths
        )
        if docs is None:
            return
        with self._handler() as env:
            with env.begin(write=True) as transaction:
                for d in docs[access_paths]:
                    # TODO figure out if there is a better way to do update in LMDB
                    # issue: the defacto update method is an upsert (if a value didn't exist, it is created)
                    # see https://lmdb.readthedocs.io/en/release/#lmdb.Cursor.replace
                    if transaction.delete(d.id.encode()):
                        transaction.replace(d.id.encode(), d.to_bytes())

    @requests(on='/delete')
    def delete(self, docs: DocumentArray, parameters: Dict, **kwargs):
        """Delete entries from the index by id

        :param docs: the documents to delete
        :param parameters: parameters to the request
        """
        access_paths = parameters.get(
            'access_paths', self.default_access_paths
        )
        if docs is None:
            return
        with self._handler() as env:
            with env.begin(write=True) as transaction:
                for d in docs[access_paths]:
                    transaction.delete(d.id.encode())

    @requests(on='/search')
    def search(self, docs: DocumentArray, parameters: Dict, **kwargs):
        """Retrieve Document contents by ids

        :param docs: the list of Documents (they only need to contain the ids)
        :param parameters: the parameters for this request
        """
        access_paths = parameters.get(
            'access_paths', self.default_access_paths
        )
        return_embeddings = parameters.get(
            'return_embeddings', self.default_return_embeddings
        )
        if docs is None:
            return
        docs_to_get = docs[access_paths]
        with self._handler() as env:
            with env.begin(write=False) as transaction:
                for i, d in enumerate(docs_to_get):
                    scores = d.scores
                    tags = d.tags
                    serialized_doc = Document.from_bytes(transaction.get(d.id.encode()))
                    if not return_embeddings:
                        serialized_doc.pop('embedding')
                    # should keep existing tags, scores etc.
                    d.copy_from(serialized_doc)
                    d.scores = scores
                    d.tags.update(tags)

    @requests(on='/dump')
    def dump(self, parameters: Dict, **kwargs):
        """Dump data from the index

        Requires
        - dump_path
        - shards
        to be part of `parameters`

        :param parameters: parameters to the request"""
        path = parameters.get('dump_path', None)
        if path is None:
            self.logger.error('parameters["dump_path"] was None')
            return

        shards = parameters.get('shards', None)
        if shards is None:
            self.logger.error('parameters["shards"] was None')
            return
        shards = int(shards)

        export_dump_streaming(path, shards, self.size, self._dump_generator())

    @property
    def size(self):
        """Compute size (nr of elements in lmdb)"""
        with self._handler() as env:
            with env.begin(write=True) as transaction:
                stats = transaction.stat()
                return stats['entries']

    def _dump_generator(self):
        with self._handler() as env:
            with env.begin(write=True) as transaction:
                cursor = transaction.cursor()
                cursor.iternext()
                iterator = cursor.iternext(keys=True, values=True)
                for it in iterator:
                    id, data = it
                    doc = Document.from_bytes(data)
                    yield id.decode(), doc.embedding, LMDBStorage._doc_without_embedding(
                        doc
                    ).to_bytes()

    @staticmethod
    def _doc_without_embedding(d):
        new_doc = Document(d, copy=True)
        new_doc.pop('embedding')
        return new_doc

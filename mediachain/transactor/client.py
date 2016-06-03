from grpc.beta import implementations

from mediachain.datastore.data_objects import Artefact, Entity, ChainCell, \
    MultihashReference
from mediachain.proto import Transactor_pb2

TIMEOUT_SECS = 120


def assert_canonical(record):
    assert (isinstance(record, Artefact) or isinstance(record, Entity)), \
           "Expected artefact or entity, got " + str(type(record))


def assert_chaincell(cell):
    assert isinstance(cell, ChainCell), \
           "Expected chain cell, got " + str(type(cell))


class TransactorClient(object):

    def __init__(self, host, port):
        channel = implementations.insecure_channel(host, port)
        self.client = Transactor_pb2.beta_create_TransactorService_stub(channel)

    def insert_canonical(self, record, timeout=TIMEOUT_SECS):
        assert_canonical(record)
        req = Transactor_pb2.InsertRequest(canonicalCbor=record.to_cbor_bytes())
        ref = self.client.InsertCanonical(req, timeout)
        return MultihashReference.from_base58(ref.reference)

    def update_chain(self, cell, timeout=TIMEOUT_SECS):
        assert_chaincell(cell)
        req = Transactor_pb2.UpdateRequest(chainCellCbor=cell.to_cbor_bytes())
        ref = self.client.UpdateChain(req, timeout)
        return MultihashReference.from_base58(ref.reference)

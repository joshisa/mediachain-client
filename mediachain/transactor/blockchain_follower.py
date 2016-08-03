import threading
import time
from base58 import b58encode
from Queue import Queue, Empty as QueueEmpty
from collections import deque
from mediachain.transactor.block_cache import get_block_cache
from mediachain.proto import Transactor_pb2  # pylint: disable=no-name-in-module
from mediachain.datastore.utils import ref_base58
from mediachain.rpc.utils import with_retry
from grpc.beta.interfaces import StatusCode
from grpc.framework.interfaces.face.face import AbortionError


class BlockchainFollower(object):
    def __init__(self,
                 stream_func,
                 catchup = True,
                 block_cache = None,
                 event_map_fn = None,
                 max_retry=20):
        if block_cache is None:
            block_cache = get_block_cache()

        self.max_retry = max_retry
        self.cache = block_cache
        self.stream_func = stream_func
        self.block_ref_queue = Queue()
        self.incoming_event_queue = Queue()
        self.caught_up = False
        self.should_catchup = catchup
        self.first_incoming_event_received = False
        self.replay_stack = deque()
        self._cancelled = False
        if event_map_fn is None:
            self.event_map_fn = lambda x: x
        else:
            self.event_map_fn = event_map_fn
        self.catchup_thread = threading.Thread(
                name='blockchain-catchup',
                target=self._perform_catchup)
        self.incoming_event_thread = threading.Thread(
            name='journal-stream-listener',
            target=self._receive_incoming_events)
        self._event_iterator = self._event_stream()

    def __iter__(self):
        return self

    def next(self):
        return next(self._event_iterator)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cancel()
        return False

    def start(self):
        self.catchup_thread.start()
        self.incoming_event_thread.start()

    def cancel(self):
        # print('BlockchainFollower cancel')
        self._cancelled = True
        self.block_ref_queue.put('__abort__')
        self.incoming_event_queue.put('__abort__')

    def _perform_catchup(self):
        if not self.should_catchup:
            return

        while True:
            ref = self.block_ref_queue.get()
            if ref == '__abort__':
                return

            block = self.cache.get(ref)

            if block is None:
                print('Could not get block with ref {}'.format(ref))
                return

            self.replay_stack.appendleft(ref)

            chain = chain_ref(block)
            if chain is None:
                # print('Reached genesis block {}'.format(ref))
                self.caught_up = True
                return
            self.block_ref_queue.put(chain)

    def _receive_incoming_events(self):
        def event_receive_worker():
            try:
                stream = self.stream_func()
                for event in stream:
                    if getattr(self, '_cancelled'):
                        stream.cancel()
                        return

                    if not self.first_incoming_event_received:
                        self.first_incoming_event_received = True
                        block_ref = block_event_ref(event)
                        if block_ref is None:
                            self.block_ref_queue.put('__abort__')
                            self.caught_up = True
                        else:
                            self.block_ref_queue.put(block_ref)

                    self.incoming_event_queue.put(event)
            except AbortionError as e:
                if self._cancelled and e.code == StatusCode.CANCELLED:
                    return
                else:
                    raise
        with_retry(event_receive_worker, max_retry_attempts=self.max_retry)

    def _event_stream(self):
        # block until catchup thread completes
        self.catchup_thread.join()

        for block_ref in self.replay_stack:
            block = self.cache.get(block_ref)
            entries = block.get('entries', [])
            for e in entries:
                e = self.event_map_fn(block_event_to_rpc_event(e))
                if e is not None:
                    yield e
            block_event = Transactor_pb2.JournalEvent()
            block_event.journalBlockEvent.reference = ref_base58(block_ref)
            block_event = self.event_map_fn(block_event)
            if block_event is not None:
                yield block_event

        while True:
            try:
                e = self.incoming_event_queue.get(block=False, timeout=1)
                if e == '__abort__':
                    return
                e = self.event_map_fn(e)
                if e is not None:
                    yield e
            except QueueEmpty:
                time.sleep(0.2)


def chain_ref(block):
    try:
        ref_bytes = bytes(block['chain']['@link'])
        return b58encode(ref_bytes)
    except (KeyError, ValueError):
        return None


def block_event_ref(rpc_event):
    if rpc_event.WhichOneof('event') != 'journalBlockEvent':
        return None
    return rpc_event.journalBlockEvent.reference


def block_event_to_rpc_event(event):
    rpc_event = Transactor_pb2.JournalEvent()
    if event['type'] == 'insert':
        rpc_event.insertCanonicalEvent.reference = ref_base58(event['ref'])
    elif event['type'] == 'update':
        rpc_event.updateChainEvent.canonical.reference = ref_base58(
            event['ref'])
        rpc_event.updateChainEvent.chain.reference = ref_base58(event['chain'])
        if 'chainPrevious' in event:
            rpc_event.updateChainEvent.chainPrevious.reference = ref_base58(
                event['chainPrevious'])
    else:
        raise ValueError('unknown journal event type: {}'.format(
            event['type']
        ))
    return rpc_event
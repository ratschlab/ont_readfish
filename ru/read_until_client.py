"""read_until_client.py
Subclasses ONTs read_until_api ReadUntilClient added extra function that logs unblocks read_ids.
"""
import logging
import queue
import time
from collections import OrderedDict
from collections.abc import MutableMapping
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from threading import RLock

from minknow_api.acquisition_pb2 import MinknowStatus
from minknow_api import protocol_service
from read_until import ReadUntilClient
from ru.utils import setup_logger
from grpc import RpcError

log = setup_logger(
    __name__,
    level=logging.INFO,
    log_format="%(asctime)s %(name)s %(message)s",
)


class RUClient(ReadUntilClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.logger.disabled = True
        self.current_phase = self.connection.protocol.get_current_protocol_run().phase
        self.phase_errors = 0
        self.max_phase_errors = 1

        # We always want one_chunk to be False
        self.one_chunk = False

        self.mk_run_dir = (
            self.connection.protocol.get_current_protocol_run().output_path
        )
        if self.mk_host not in ("localhost", "127.0.0.1"):
            # running remotely, output in cwd
            self.mk_run_dir = "."

        # Creates the output directory with 777 permissions
        Path(self.mk_run_dir).mkdir(parents=True, exist_ok=True)

        # Attempt to create `unblocked_read_ids.txt` if this fails set the run
        # directory as the PWD this will also affect where the channels.toml
        # file is written to
        try:
            ids_log = Path(self.mk_run_dir).joinpath("unblocked_read_ids.txt")
            ids_log.touch(exist_ok=True)
        except PermissionError:
            # TODO: log message here that fallback output is in use
            self.mk_run_dir = "."
            ids_log = Path(self.mk_run_dir).joinpath("unblocked_read_ids.txt")
            ids_log.touch(exist_ok=True)

        self.log_queue = queue.Queue(-1)
        self.queue_handler = QueueHandler(self.log_queue)
        self.unblock_logger = logging.getLogger("unblocks")
        self.unblock_logger.setLevel(logging.DEBUG)
        self.unblock_logger.propagate = False
        self.unblock_logger.addHandler(self.queue_handler)
        fmt = logging.Formatter("%(message)s")
        self.file_handler = logging.FileHandler(str(ids_log), mode="a")
        self.file_handler.setFormatter(fmt)
        self.listener = QueueListener(self.log_queue, self.file_handler)
        self.listener.start()

        while (
            self.connection.acquisition.current_status().status
            != MinknowStatus.PROCESSING
        ):
            time.sleep(1)

    def unblock_read_batch(self, reads, duration=0.1):
        """Request for a bunch of reads be unblocked.
        reads is expected to be a list of (channel, ReadData.number)
        :param reads: List of (channel, read_number, read_id)
        :type reads: list(tuple)
        :param duration: time in seconds to apply unblock voltage.
        :type duration: float
        :returns: None
        """
        actions = list()
        for channel, read_number, read_id in reads:
            actions.append(
                self._generate_action(
                    channel, read_number, "unblock", duration=duration
                )
            )
            self.unblock_logger.debug(read_id)
        self.action_queue.put(actions)

    def unblock_read(self, read_channel, read_number, duration=0.1, read_id=None):
        super().unblock_read(
            read_channel=read_channel,
            read_number=read_number,
            duration=duration,
        )
        if read_id is not None:
            self.unblock_logger.debug(read_id)

    @property
    def is_phase_sequencing(self):
        """
        Check the current protocol phase to determine if the run is not paused/muxing/unknown

        :returns: Bool
        """
        try:
            current_phase = self.connection.protocol.get_current_protocol_run().phase
        except RpcError as e:
            if self.phase_errors < self.max_phase_errors:
                log.info(f"Got RPC exception\n{e}")
                log.info("Run may have ended")
                self.phase_errors += 1
            return False

        if current_phase != self.current_phase:
            self.current_phase = current_phase
            log.info(
                f"Protocol phase changed to {protocol_service.ProtocolPhase.Name(self.current_phase)}"
            )
        return current_phase == protocol_service.PHASE_SEQUENCING

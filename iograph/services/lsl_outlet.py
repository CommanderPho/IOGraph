"""LSL outlet stream for broadcasting IOGraph mouse coordinates."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pylsl import StreamInfo, StreamOutlet

logger = logging.getLogger(__name__)

try:
    import pylsl
    from pylsl import StreamInfo, StreamOutlet
    from phopylslhelper.easy_time_sync import EasyTimeSyncParsingMixin
    lsl_available = True
except ImportError:
    lsl_available = False
    logger.warning("pylsl or phopylslhelper not available. LSL tracking will be disabled.")

    class EasyTimeSyncParsingMixin:
        def init_EasyTimeSyncParsingMixin(self) -> None:
            pass

        def EasyTimeSyncParsingMixin_add_lsl_outlet_info(self, info: Any) -> Any:
            return info
            
        def capture_stream_start_timestamps(self) -> None:
            pass
            
        def capture_recording_start_timestamps(self) -> None:
            pass


class IOGraphLSLOutlet(EasyTimeSyncParsingMixin):
    """LSL stream for sending continuous mouse coordinates."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and lsl_available
        self.outlet: StreamOutlet | None = None
        
        if self.enabled:
            try:
                self.init_EasyTimeSyncParsingMixin()
                self._create_stream()
                logger.info("LSL position stream created successfully.")
            except Exception as e:
                logger.warning(f"Failed to create LSL stream: {e}. Continuing without LSL.")
                self.enabled = False
        else:
            if not lsl_available:
                logger.info("LSL not available (pylsl not installed)")
            else:
                logger.info("LSL disabled in configuration")
    

    def add_lsl_outlet_info_common(self, info: StreamInfo) -> StreamInfo:
        """Adds common LSL metadata and phopylslhelper time-sync fields."""
        info.desc().append_child_value("manufacturer", "IOGraph")
        info.desc().append_child_value("version", "2.0.0")
        info.desc().append_child_value("description", "IOGraph mouse coordinates")

        # add a custom timestamp field to the stream info:
        info = self.EasyTimeSyncParsingMixin_add_lsl_outlet_info(info=info)
        return info
    

    def get_lsl_outlet_stream_info(self) -> StreamInfo:
        assert lsl_available and pylsl is not None and StreamInfo is not None
        
        info = StreamInfo(
            name="IOGraph_Mouse",
            type="Position",
            channel_count=2,
            nominal_srate=pylsl.IRREGULAR_RATE,
            channel_format=pylsl.cf_float32,
            source_id="iograph_mouse_tracker"
        )

        info = self.add_lsl_outlet_info_common(info=info)
        return info


    def _create_stream(self) -> None:
        """Create LSL stream outlet."""
        if not lsl_available:
            return
        info = self.get_lsl_outlet_stream_info()
        self.outlet = pylsl.StreamOutlet(info)
        logger.info(f"LSL stream created with info: {info}")
    

    def push_sample(self, x: float, y: float) -> None:
        """Send a single coordinate sample to LSL."""
        if not self.enabled or self.outlet is None:
            return
        
        try:
            self.outlet.push_sample([x, y])
        except Exception as e:
            logger.warning(f"Failed to push LSL coordinate sample: {e}")


    def close(self) -> None:
        """Close LSL stream outlet."""
        if self.outlet is not None:
            try:
                self.outlet = None
                logger.info("LSL stream closed")
            except Exception as e:
                logger.warning(f"Error closing LSL stream: {e}")

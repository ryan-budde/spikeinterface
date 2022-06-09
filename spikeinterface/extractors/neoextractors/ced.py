from spikeinterface.core.core_tools import define_reader_function

from .neobaseextractor import NeoBaseRecordingExtractor, NeoBaseSortingExtractor


class CedRecordingExtractor(NeoBaseRecordingExtractor):
    """
    Class for reading smr/smrw CED file.

    Based on neo.rawio.CedRawIO / sonpy
    
    Alternative to read_spike2 which do not handle smrx
    
    Parameters
    ----------
    file_path: str
        The smr or smrx  file.
    stream_id: str or None
    """
    mode = 'file'
    NeoRawIOClass = 'CedRawIO'

    def __init__(self, file_path, stream_id=None):
        neo_kwargs = {'filename': str(file_path)}
        NeoBaseRecordingExtractor.__init__(self, stream_id=stream_id, **neo_kwargs)

        self._kwargs = dict(file_path=str(file_path), stream_id=stream_id)


read_ced = define_reader_function(source_class=CedRecordingExtractor, name="read_ced")

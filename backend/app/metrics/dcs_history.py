"""兼容旧导入路径；DCS 历史仅使用原生接口实际采样。"""
from app.metrics.dcs_samples import RawMemoryPoint, finite_number

__all__ = ["RawMemoryPoint", "finite_number"]

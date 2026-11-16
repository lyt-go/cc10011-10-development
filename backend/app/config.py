"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    app_name: str = "实时指标计算与监控大盘平台"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 检测模型的训练/部署环境口径。两边一致才允许上线；
    # 改这里等价于换了一个运行时，已训练模型需要在部署前重新训练。
    training_runtime: str = "metric-train-runtime/1.0"
    model_runtime: str = "anomaly-detect-runtime/1.4"
    metric_timezone: str = "UTC"


settings = Settings()

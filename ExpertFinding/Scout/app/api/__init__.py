"""内部同步 JSON API（Phase 1，内部/低频使用）。

分层约定：

* :mod:`app.api.app` — 路由与 HTTP 编解码，不承载领域逻辑；
* :mod:`app.api.auth` — API Key 常量时间校验、敏感输入拒绝、调用预算；
* :mod:`app.api.schemas` — 外部请求/响应 DTO 与边界校验；
* :mod:`app.api.errors` — 稳定错误码与 HTTP 状态映射。

本包不持有会话材料：Cookie / msToken / storage / 签名输入既不接受为输入，
也不会出现在任何响应或错误体里。
"""

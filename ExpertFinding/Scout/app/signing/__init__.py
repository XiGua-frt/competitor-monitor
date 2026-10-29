"""签名边界：Signer Protocol 与只读请求的签名后备实现。

当前 /api/search/general/full/ 已验证无需 X-Bogus，因此首期只提供
NoopSigner(none-v1)；将来端点收紧时按同一 Protocol 插入真实实现。
"""

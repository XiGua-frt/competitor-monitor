"""授权会话边界：导入、元数据、撤销/到期检查与内存 vault。

边界：本包只处理会话的"元数据与状态"，任何明文 Cookie / msToken 都不会
进入 SessionMeta、public_status()、repr 或磁盘。明文材料只在需要时由
transport 层自行从 .secrets/tiktok_session.json 读取。
"""

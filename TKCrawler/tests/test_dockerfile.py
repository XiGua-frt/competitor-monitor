#!/usr/bin/env python
"""Dockerfile 静态校验 (Agent N)。

纯文本断言, 不依赖 Docker daemon。校验 Dockerfile/.dockerignore 与
docker-compose.yml crawler 服务 command 的关键约定是否保持一致。
"""

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    assert DOCKERFILE.exists(), "项目根缺少 Dockerfile"
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dockerignore_text() -> str:
    assert DOCKERIGNORE.exists(), "项目根缺少 .dockerignore"
    return DOCKERIGNORE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dockerfile 关键行
# ---------------------------------------------------------------------------
def test_base_image_is_python_311_slim(dockerfile_text: str) -> None:
    assert re.search(r"^FROM python:3\.11-slim\s*$", dockerfile_text, re.M), \
        "基础镜像必须是 python:3.11-slim"


def test_requirements_copied_before_app_code(dockerfile_text: str) -> None:
    """依赖层缓存友好: COPY requirements.txt 与 pip install 必须先于 COPY src/。"""
    copy_req = dockerfile_text.index("COPY requirements.txt")
    pip_install = dockerfile_text.index("pip install")
    copy_src = dockerfile_text.index("COPY src/")
    assert copy_req < pip_install < copy_src, \
        "应先 COPY requirements.txt 并 pip install, 再 COPY 应用代码"


def test_required_copy_targets_present(dockerfile_text: str) -> None:
    for target in ("COPY src/", "COPY scripts/", "COPY main.py"):
        assert target in dockerfile_text, f"缺少 {target}"


def test_no_entrypoint(dockerfile_text: str) -> None:
    """compose 的 command `python main.py scheduler --start` 是完整命令,
    会覆盖 CMD 但保留 ENTRYPOINT; 若设 ENTRYPOINT 会被拼接成
    `python main.py python main.py ...` 而失败, 故禁止 ENTRYPOINT。"""
    assert not re.search(r"^\s*ENTRYPOINT", dockerfile_text, re.M), \
        "Dockerfile 不得设置 ENTRYPOINT (compose command 语义冲突)"


def test_cmd_is_python_main(dockerfile_text: str) -> None:
    assert re.search(r'^CMD \["python", "main\.py"\]$', dockerfile_text, re.M), \
        'CMD 兜底默认值应为 ["python", "main.py"]'


def test_non_root_user(dockerfile_text: str) -> None:
    assert re.search(r"^\s*USER\s+crawler\s*$", dockerfile_text, re.M), \
        "必须以非 root 用户 (crawler) 运行"


def test_workdir_app(dockerfile_text: str) -> None:
    assert re.search(r"^\s*WORKDIR\s+/app\s*$", dockerfile_text, re.M)


# ---------------------------------------------------------------------------
# 依赖补偿与包过滤 (requirements.txt 禁改前提下的构建期处理)
# ---------------------------------------------------------------------------
def test_amazon_paapi_filtered(dockerfile_text: str) -> None:
    """amazon-paapi 在 PyPI 不存在 (404), 构建期必须过滤。"""
    assert "amazon-paapi" in dockerfile_text, \
        "pip 安装行应保留 amazon-paapi 过滤逻辑"


def test_psycopg3_installed(dockerfile_text: str) -> None:
    """SQLAlchemy 2.x 默认 DBAPI 为 psycopg3, 需补装 psycopg[binary]。"""
    assert re.search(r'psycopg\[binary\]', dockerfile_text), \
        "应补装 psycopg[binary] (SQLAlchemy 2.x 默认 DBAPI)"


def test_build_tools_purged(dockerfile_text: str) -> None:
    """gcc/g++ 等构建工具必须在同一 RUN 内 purge, 不残留镜像层。"""
    assert re.search(r"apt-get purge -y gcc g\+\+", dockerfile_text), \
        "构建工具需在同一 RUN 内清除"


# ---------------------------------------------------------------------------
# .dockerignore: 凭据与无关内容严禁进入构建上下文
# ---------------------------------------------------------------------------
def test_env_excluded(dockerignore_text: str) -> None:
    lines = [ln.strip() for ln in dockerignore_text.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert ".env" in lines, ".dockerignore 必须排除 .env (真实凭据严禁进镜像层)"


def test_negated_env_example_kept(dockerignore_text: str) -> None:
    assert "!.env.example" in dockerignore_text, \
        "应保留 .env.example 作为参考模板"


@pytest.mark.parametrize("excluded", [
    ".git", "tests/", "docs/", "data/", "logs/", "exports/",
    "__pycache__/", ".workbuddy/",
])
def test_noise_excluded(dockerignore_text: str, excluded: str) -> None:
    assert excluded in dockerignore_text, f".dockerignore 应排除 {excluded}"


# ---------------------------------------------------------------------------
# 与 docker-compose.yml crawler 服务的一致性 (只读, 不修改)
# ---------------------------------------------------------------------------
def test_compose_command_matches_semantics() -> None:
    compose = PROJECT_ROOT / "docker-compose.yml"
    assert compose.exists()
    text = compose.read_text(encoding="utf-8")
    assert re.search(r"^\s*command:\s*python main\.py scheduler --start\s*$", text, re.M), \
        "compose crawler command 变更时需同步复核 Dockerfile 的 ENTRYPOINT 约定"

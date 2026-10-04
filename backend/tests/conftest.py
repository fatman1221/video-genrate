"""pytest 全局夹具。

设计要点
--------
1. **完全隔离**：整场测试跑在系统临时目录下的独立 SQLite 上。环境变量必须在
   ``import app.*`` **之前**设好 —— ``app/config.py`` 在模块导入时就固化了 settings
   （``settings = get_settings()``），之后再改 env 是无效的。
2. **绝不启动 worker**：只做 ``init_db`` + 注册 Provider / Handler，**不调用**
   ``executors.runner.start()``。否则测试会真的去跑生成任务（消耗 GPU、产出垃圾）。
   HTTP 用例用 ``TestClient(app)`` 但**不进上下文管理器**，因此 lifespan 不执行、
   worker 不启动。
3. **两层隔离粒度**：
   - 服务层用例走 ``db`` 夹具：**只用 flush、不提交**，用例结束统一 rollback。
     新增的 5 个 service 模块都不自己 commit，所以这是可靠的。
   - HTTP 用例走 Skill 层，它**会 commit**（``skills/base.py``）。因此这类用例各自
     通过 ``bootstrap_project`` 建独立项目，靠 project_id 隔离而非事务隔离。
4. ``app.main`` 在导入时会 ``StaticFiles(directory=storage)``，目录不存在直接抛错，
   所以必须**先建目录再导入**。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# --- ① 必须在 import app 之前落地环境 ------------------------------------- #
_TMP = Path(tempfile.mkdtemp(prefix="vg_pytest_"))
(_TMP / "storage").mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["STORAGE_ROOT"] = str(_TMP / "storage")
os.environ["DB_ECHO"] = "false"
os.environ["SIMULATE_LATENCY"] = "false"
# 不启动 worker；即便有人误进 lifespan 也不会拉起生成任务
os.environ["TASK_WORKERS"] = "0"

import pytest  # noqa: E402

from app.database import SessionLocal, engine, init_db  # noqa: E402


# --------------------------------------------------------------------------- #
# 一次性启动
# --------------------------------------------------------------------------- #
def _boot() -> None:
    from app.executors import load_handlers
    from app.providers import register_all, sync_providers_table

    init_db()
    register_all()
    load_handlers()
    db = SessionLocal()
    try:
        sync_providers_table(db)
        db.commit()
    finally:
        db.close()


@pytest.fixture(scope="session", autouse=True)
def _session_boot():
    _boot()
    yield
    engine.dispose()
    shutil.rmtree(_TMP, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 服务层：不提交的会话（新 service 都只 flush）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def fx(db) -> SimpleNamespace:
    """最小可用项目图：项目 / 分镜 / 场景 / 镜头 / 角色 / 造型 / 地点 / 视图 / 风格。

    所有 id 带随机后缀，project 内唯一 —— 避免与同场其它用例（HTTP 侧会提交）
    的数据撞唯一约束。
    """
    from app import models as m
    from app.services import visual_bible as vb

    uid = uuid.uuid4().hex[:8]
    proj = m.Project(id=f"proj_t_{uid}", name="测试项目", width=1280, height=720,
                     aspect_ratio="16:9")
    db.add(proj)
    db.flush()
    sb = m.Storyboard(id=f"sb_t_{uid}", project_id=proj.id, title="分镜")
    db.add(sb)
    db.flush()
    scn = m.Scene(id=f"scn_t_{uid}", project_id=proj.id, storyboard_id=sb.id,
                  sequence=1, code="SC001")
    db.add(scn)
    db.flush()
    shot = m.Shot(id=f"shot_t_{uid}", project_id=proj.id, scene_id=scn.id,
                  sequence=1, code="001",
                  description="林野在舱内舷窗前回头", camera="缓慢推近", duration=7.0)
    ch = m.Character(id=f"chr_t_{uid}", project_id=proj.id, name=f"林野{uid}",
                     appearance="三十岁男性，短寸头", code=f"CHAR-{uid.upper()}")
    db.add_all([shot, ch])
    db.flush()

    bible = vb.update_bible(
        db, proj, visual_logline="冷调静谧的深空舱内",
        global_rules={"stage_policy": "环境内展示，保持舷窗可见"},
        text_policy={"readable_text_allowed": True},
    )
    style = vb.create_style(
        db, proj, name="写实电影感", form_card="live_action",
        rendering={"surface": "细腻金属与织物纹理"},
        lighting={"key": "冷调侧逆光"},
        palette={"primary": "青灰", "accent": "暖橙"},
        set_current=True,
    )
    ch.identity_anchors = ["方额窄下颌", "后颈发际收成尖角"]
    db.flush()
    look = vb.create_look(
        db, ch, name="常服",
        differences={"wardrobe_layers": ["深灰立领拉链夹克", "黑色工装裤"]},
        is_current=True,
    )
    loc = vb.create_location(
        db, proj, name="守望者舱",
        spatial_identity={"shape": "长方形舱室", "fixed_anchors": ["圆形舷窗", "控制台"]},
    )
    view = vb.create_location_view(
        db, loc, name="舷窗北向夜", orientation={"toward": "舷窗"},
        state_differences={"time": "深夜", "light": ["冷白屏幕光"]},
        is_current=True,
    )
    vb.set_shot_bindings(
        db, shot,
        [{"kind": "character", "id": ch.id, "variant_id": look.id, "role": "主角"}],
    )
    vb.set_shot_location(db, shot, loc)
    db.flush()

    return SimpleNamespace(
        project=proj, storyboard=sb, scene=scn, shot=shot, character=ch,
        look=look, location=loc, view=view, style=style, bible=bible,
    )


# --------------------------------------------------------------------------- #
# HTTP 层：ASGI 直连（不起服务、不进 lifespan）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    # ⚠️ 不用 `with TestClient(app) as c`：那会跑 lifespan → 启动 worker → 真的出图
    return TestClient(app)


def skill(client, name: str, payload: dict | None = None) -> dict:
    """调用 Skill 并解开信封，只返回 ``data``；失败直接抛断言错误。"""
    resp = client.post(f"/api/skills/{name}/invoke", json=payload or {})
    assert resp.status_code == 200, f"{name} HTTP {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("ok"), f"{name} 调用失败：{body.get('error')}"
    return body.get("data") or {}


@pytest.fixture()
def api_project(client) -> dict:
    """HTTP 用例的独立项目（会提交到库，靠 project_id 隔离）。"""
    return skill(client, "bootstrap_project", {
        "name": f"测试项目-{uuid.uuid4().hex[:6]}",
        "requirement": "验证迁移后既有链路可用",
        "target_duration": 30,
        "shot_duration": 5,
        "create_characters": True,
        "generate_references": False,
    })

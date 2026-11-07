"""网页控制台后端（FastAPI）。

安全设计：
  * 默认只监听 127.0.0.1，外网访问不到。若显式改成 0.0.0.0，启动时会打警告。
  * 任何接口都不返回私钥；Telegram token 只回传"是否已配置"。
  * 所有下单/平仓动作都在接口里二次确认（前端弹确认框 + 后端只接受明确的布尔参数）。
  * 钱包相关接口全部只读：用地址查公开账户数据，不签名、不下单、不接收任何私钥。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from bot.manager import SlotManager
from bot.utils import now_ms
from bot.wallet import get_reader, is_address

STATIC_DIR = Path(__file__).resolve().parent / "static"
PUSH_INTERVAL_SEC = 1.5
EQUITY_TAIL_IN_STREAM = 240  # 实时推送里只带最近这么多点，历史曲线走单独接口

app = FastAPI(title="Hyperliquid 网格交易台", docs_url=None, redoc_url=None)

mgr: Optional[SlotManager] = None


def init(root: Path) -> None:
    global mgr
    mgr = SlotManager(root)


def _m() -> SlotManager:
    if mgr is None:
        raise HTTPException(500, "管理器未初始化")
    return mgr


def _light(snap: Dict[str, Any]) -> Dict[str, Any]:
    """裁剪快照，避免实时推送里塞 4000 个权益点。"""
    if not snap:
        return snap
    s = dict(snap)
    eq = s.get("equity") or []
    s["equity"] = eq[-EQUITY_TAIL_IN_STREAM:]
    s["equity_total"] = len(eq)
    return s


# =====================================================================
#                              页面
# =====================================================================
@app.get("/")
def index():
    f = STATIC_DIR / "index.html"
    if not f.exists():
        raise HTTPException(500, "缺少前端文件 web/static/index.html")
    # 把静态资源的最新修改时间注入成版本号：改了 app.js/style.css 之后，
    # 浏览器刷新就一定拿到新文件，不会抱着旧缓存不放。
    try:
        stamp = int(max((p.stat().st_mtime for p in STATIC_DIR.glob("*") if p.is_file()), default=0))
    except Exception:
        stamp = 0
    html = f.read_text(encoding="utf-8").replace("__STATIC_V__", str(stamp))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


class _NoCacheStatic(StaticFiles):
    """静态资源一律回源校验，配合上面的版本号彻底避免看到旧前端。"""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


if STATIC_DIR.exists():
    app.mount("/static", _NoCacheStatic(directory=str(STATIC_DIR)), name="static")


# =====================================================================
#                              总览
# =====================================================================
@app.get("/api/health")
def health():
    return {"ok": True, "ts": now_ms()}


@app.get("/api/overview")
def overview():
    return _m().overview()


@app.get("/api/slots")
def list_slots():
    return {"slots": _m().list_slots()}


@app.get("/api/stream")
def stream():
    """SSE 实时推送：总览 + 所有实例状态。前端 EventSource 会自动重连。"""

    def gen():
        while True:
            try:
                m = _m()
                slots = m.list_slots()
                payload = {
                    "ts": now_ms(),
                    "overview": m.overview(),
                    "slots": slots,
                    "detail": {s["name"]: _light(m.snapshot(s["name"])) for s in slots},
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            except GeneratorExit:
                return
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            time.sleep(PUSH_INTERVAL_SEC)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# =====================================================================
#                            单实例查询
# =====================================================================
@app.get("/api/slots/{name}")
def slot_snapshot(name: str):
    try:
        return _m().snapshot(name)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/slots/{name}/logs")
def slot_logs(name: str, n: int = 200):
    try:
        return {"logs": _m().logs(name, max(1, min(n, 400)))}
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/slots/{name}/equity")
def slot_equity(name: str, limit: int = 2000):
    try:
        snap = _m().snapshot(name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    eq = snap.get("equity") or []
    return {"equity": eq[-max(10, min(limit, 4000)):]}


# =====================================================================
#                              控制
# =====================================================================
def _result(ok: bool, msg: str):
    return {"ok": ok, "message": msg}


@app.post("/api/slots/{name}/start")
def slot_start(name: str):
    ok, msg = _m().start(name)
    return JSONResponse(_result(ok, msg), status_code=200 if ok else 400)


@app.post("/api/slots/{name}/stop")
def slot_stop(name: str, payload: Dict[str, Any] = Body(default={})):
    ok, msg = _m().stop(name, close_position=bool(payload.get("close", False)))
    return JSONResponse(_result(ok, msg), status_code=200 if ok else 400)


@app.post("/api/slots/{name}/cancel-all")
def slot_cancel_all(name: str):
    ok, msg = _m().cancel_all(name)
    return JSONResponse(_result(ok, msg), status_code=200 if ok else 400)


@app.post("/api/slots/{name}/close-all")
def slot_close_all(name: str):
    ok, msg = _m().close_all(name)
    return JSONResponse(_result(ok, msg), status_code=200 if ok else 400)


@app.post("/api/slots/{name}/reset-state")
def slot_reset(name: str):
    ok, msg = _m().reset_state(name)
    return JSONResponse(_result(ok, msg), status_code=200 if ok else 400)


@app.post("/api/emergency-stop")
def emergency_stop():
    """全局急停：停止所有实例 → 撤单 → 平仓。"""
    m = _m()
    results = []
    for s in m.list_slots():
        name = s["name"]
        if not s.get("running"):
            continue
        ok, msg = m.stop(name, close_position=True)
        results.append(f"{name}: {msg}" if ok else f"{name}: 失败 - {msg}")
    return {"ok": True, "message": "急停完成\n" + ("\n".join(results) if results else "没有正在运行的实例")}


# =====================================================================
#                          钱包（只读）
# =====================================================================
# 网页上「连接钱包」拿到的只是一个地址。这里用它去查 Hyperliquid 上的
# 真实账户数据，全部是公开只读接口 —— 不签名、不下单、不接触任何私钥。
@app.get("/api/wallet/{address}")
def wallet_account(address: str, network: str = "mainnet", full: int = 0):
    if not is_address(address):
        raise HTTPException(400, "钱包地址格式不正确（应为 0x 开头的 40 位十六进制）")
    try:
        data = get_reader().fetch(address, network, full=bool(int(full or 0)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"查询 Hyperliquid 账户失败：{type(e).__name__}: {e}")
    return data


@app.post("/api/wallet/bind")
def wallet_bind(payload: Dict[str, Any] = Body(default={})):
    """把连接的钱包地址写进某个实例的 env.account_address。"""
    addr = str(payload.get("address") or "").strip()
    slot = str(payload.get("slot") or "").strip()
    if not slot:
        return JSONResponse(_result(False, "请先选择要绑定的实例"), status_code=400)
    try:
        ok, msg = _m().bind_account(slot, addr)
    except KeyError:
        return JSONResponse(_result(False, f"找不到实例 {slot}"), status_code=404)
    return JSONResponse(_result(ok, msg), status_code=200 if ok else 400)


# =====================================================================
#                            配置管理
# =====================================================================
@app.put("/api/slots/{name}/config")
def slot_update_config(name: str, payload: Dict[str, Any] = Body(default={})):
    ok, msg = _m().update_config(name, payload)
    return JSONResponse(_result(ok, msg), status_code=200 if ok else 400)


@app.post("/api/slots")
def slot_create(payload: Dict[str, Any] = Body(default={})):
    ok, msg = _m().create_slot(payload.get("name", ""))
    return JSONResponse(_result(ok, msg), status_code=200 if ok else 400)


@app.delete("/api/slots/{name}")
def slot_delete(name: str):
    ok, msg = _m().delete_slot(name)
    return JSONResponse(_result(ok, msg), status_code=200 if ok else 400)


@app.post("/api/slots/{name}/notify-test")
def notify_test(name: str):
    from bot.notify import Notifier

    try:
        cfg = _m().load(name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    if not cfg.notify.telegram_bot_token or not cfg.notify.telegram_chat_id:
        return JSONResponse(_result(False, "请先填写 Telegram Bot Token 和 Chat ID 并保存"), status_code=400)
    ok = Notifier(cfg, _m()._logger(name), name).test()
    return JSONResponse(_result(ok, "测试消息已发出，请查看 Telegram" if ok else "发送失败，请检查 Token / Chat ID"),
                        status_code=200 if ok else 400)


# =====================================================================
#                              启动
# =====================================================================
def run(root: Path, host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn

    init(root)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("!" * 66)
        print(f"⚠️  正在监听 {host}，这意味着同一网络/公网可能访问到这个控制台。")
        print("⚠️  控制台能直接下单和平仓，且没有任何登录鉴权。")
        print("⚠️  如果这不是你本意，请改用 --host 127.0.0.1。")
        print("!" * 66)
    print(f"\n  交易台已启动： http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)

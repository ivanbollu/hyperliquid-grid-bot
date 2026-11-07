"""全链扫描某地址的 BLAST 代币收款历史（Blast RPC 限单次 1 万块，逐段扫描）。

结果写入 tools/_blast_scan_result.json，进度打印到 stdout。
用法: python tools/blast_transfer_scan.py 0x地址
"""
import json
import sys
import time
import urllib.request

ADDR = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
BLAST = "0xb1a5700fA2358173Fe465e6eA4Ff52E36e88E2ad"   # 官方 BLAST（Blast 链）
RPC = "https://rpc.blast.io"
D = 10 ** 18
TGE_UTC = 1719369600  # 2024-06-26 00:00 UTC（TGE/空投开放领取日）

def rpc(method, params, timeout=45):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    if "error" in d:
        raise RuntimeError(d["error"].get("message", ""))
    return d["result"]

def hx(x): return int(x, 16)

def block_ts(bn):
    return hx(rpc("eth_getBlockByNumber", [hex(bn), False])["timestamp"])

# 1) 二分找 TGE 日的区块，跳过之前的空转区间
LATEST = hx(rpc("eth_blockNumber", []))
lo, hi = 0, LATEST
while lo < hi:
    mid = (lo + hi) // 2
    if block_ts(mid) < TGE_UTC:
        lo = mid + 1
    else:
        hi = mid
start_block = max(lo - 50_000, 0)
print(f"最新区块 {LATEST:,}，TGE 起扫区块 {start_block:,}", flush=True)

# 2) 10k 一段扫收款方向
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
pad = "0x" + "0" * 24 + ADDR[2:].lower()
events, failed, done = [], 0, 0
bn_ = start_block
t0 = time.time()
while bn_ <= LATEST:
    end = min(bn_ + 9_999, LATEST)
    for attempt in range(4):
        try:
            logs = rpc("eth_getLogs", [{"address": BLAST, "fromBlock": hex(bn_),
                                        "toBlock": hex(end), "topics": [TRANSFER, None, pad]}])
            for lg in logs:
                events.append({"block": hx(lg["blockNumber"]),
                               "amount": hx(lg["data"]) / D,
                               "from": "0x" + lg["topics"][1][26:]})
            break
        except Exception:
            if attempt == 3:
                failed += 1
            time.sleep(1.5 * (attempt + 1))
    done += 1
    if done % 200 == 0:
        print(f"进度 {done} 段 / 至区块 {end:,} / 已发现 {len(events)} 笔 / 失败 {failed}", flush=True)
    bn_ = end + 1

out = {"address": ADDR, "token": BLAST, "scanned_to_block": hi,
       "start_block": start_block, "events": events, "failed_chunks": failed,
       "total_in": sum(e["amount"] for e in events)}
with open("tools/_blast_scan_result.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"\n完成：{len(events)} 笔收款，合计 {out['total_in']:,.2f} BLAST，失败区间 {failed}，"
      f"用时 {time.time()-t0:.0f}s", flush=True)

"""钱包资产扫描器：扫描某地址在 Ethereum / Blast / Arbitrum 上的原生币与常见 ERC-20 余额。

只读，不发任何交易。用法：
    python tools/scan_wallet.py [0x地址]
不传地址则扫描 .env 里的主钱包（account_address）。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

# 各链公共 RPC（按顺序尝试，前面限流/不可达就切下一个；经过实测，当前网络环境下部分可达）
CHAINS = {
    "Ethereum": {"rpcs": ["https://1rpc.io/eth", "https://rpc.flashbots.net"], "native": "ETH"},
    "Blast": {"rpcs": ["https://rpc.blast.io"], "native": "ETH"},
    "Arbitrum": {"rpcs": ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com"], "native": "ETH"},
}

# 常见代币候选（合约地址会先用 symbol()/decimals() 在链上校验，对不上就跳过）
TOKENS = {
    "Ethereum": [
        ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
        ("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7"),
        ("DAI", "0x6B175474E89094C44Da98b954EedeAC495271d0F"),
        ("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
        ("stETH", "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"),
        ("wstETH", "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"),
        ("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"),
        ("LINK", "0x514910771AF9Ca656af840dff83E8264EcF986CA"),
        ("UNI", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984"),
        ("AAVE", "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9"),
        ("USDe", "0x4c9EDD5852cd905f086C759E8383e09bff1E68B3"),
        ("ARB", "0x912CE59144191C1204E64559FE8253a0e49E6548"),
    ],
    "Blast": [
        ("WETH", "0x4300000000000000000000000000000000000004"),
        ("USDB", "0x4300000000000000000000000000000000000003"),
        ("BLAST", "0xb1a5700fA2358173Fe465e6eA4Ff52E36e88E2ad"),
        ("USDC", "0xd8266C7d98A25A0e2df4B0c5D4c6a7ebFc5EA497"),
        ("DAI", "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb"),
        ("WBTC", "0x78Efc50Fe80f780658D756A974D70c2Ff5F849a5"),
    ],
    "Arbitrum": [
        ("USDC", "0xaf88d066e678a3c0e132d4cd0d1e29c25bdb2e42"),
        ("USDC.e", "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8"),
        ("USDT", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"),
        ("WETH", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"),
        ("ARB", "0x912CE59144191C1204E64559FE8253a0e49E6548"),
        ("DAI", "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"),
    ],
}

SEL_BALANCE_OF = "0x70a08231"
SEL_SYMBOL = "0x95d89b41"
SEL_DECIMALS = "0x313ce567"


def rpc(url, method, params, timeout=25, _tries=3):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err = None
    for i in range(_tries):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) scan_wallet/1.0",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            if "error" in d:
                raise RuntimeError(d["error"].get("message", str(d["error"])))
            return d["result"]
        except Exception as e:  # 403/429 多为限流，退避后重试
            last_err = e
            time.sleep(0.8 * (i + 1))
    raise last_err


def call_str(url, contract, selector):
    """解码 string 返回值（symbol），失败返回 None。"""
    try:
        raw = rpc(url, "eth_call", [{"to": contract, "data": selector}, "latest"])
        if not raw or raw == "0x":
            return None
        b = bytes.fromhex(raw[2:])
        if len(b) >= 64:
            off = int.from_bytes(b[0:32], "big")
            if off in (32, 64):  # 动态 string 编码
                ln = int.from_bytes(b[off:off + 32], "big")
                return b[off + 32:off + 32 + ln].decode("utf-8", "replace")
        return b.rstrip(b"\x00").decode("utf-8", "replace")
    except Exception:
        return None


def call_uint(url, contract, selector):
    try:
        raw = rpc(url, "eth_call", [{"to": contract, "data": selector}, "latest"])
        return int(raw, 16) if raw and raw != "0x" else None
    except Exception:
        return None


def token_balance(url, contract, addr):
    data = SEL_BALANCE_OF + "000000000000000000000000" + addr[2:].lower()
    raw = rpc(url, "eth_call", [{"to": contract, "data": data}, "latest"])
    return int(raw, 16) if raw and raw != "0x" else 0


def scan(chain, addr, native_px):
    cfg = CHAINS[chain]
    print(f"\n{'=' * 70}\n[{chain}]\n{'=' * 70}")
    url = None
    for cand in cfg["rpcs"]:
        try:
            rpc(cand, "eth_getBalance", [addr, "latest"], timeout=15, _tries=1)
            url = cand
            print(f"  节点: {cand}")
            break
        except Exception:
            continue
    if url is None:
        print("  所有节点均不可达，跳过该链")
        return None
    try:
        wei = int(rpc(url, "eth_getBalance", [addr, "latest"]), 16)
        nonce = int(rpc(url, "eth_getTransactionCount", [addr, "latest"]), 16)
        code = rpc(url, "eth_getCode", [addr, "latest"])
    except Exception as e:
        print(f"  RPC 查询失败：{e}")
        return None
    eth = wei / 1e18
    kind = "合约" if code and code != "0x" else "EOA(普通钱包)"
    print(f"  原生币 {cfg['native']:<4} : {eth:>14.8f}   ≈ ${eth * native_px:,.2f}")
    print(f"  交易笔数(nonce) : {nonce}    账户类型: {kind}")
    found = []
    for name, contract in TOKENS[chain]:
        try:
            bal = token_balance(url, contract, addr)
        except Exception as e:  # 单个代币查询失败不中断整链扫描
            print(f"  代币 {name:<8}: 查询失败（{str(e)[:50]}），跳过")
            continue
        if bal <= 0:
            time.sleep(0.15)
            continue
        sym = call_str(url, contract, SEL_SYMBOL) or name
        dec = call_uint(url, contract, SEL_DECIMALS)
        dec = dec if dec is not None else 18
        qty = bal / (10 ** dec)
        found.append((sym, qty, contract))
        print(f"  代币 {sym:<8}: {qty:>18,.6f}   合约 {contract}")
    if not found:
        print("  （扫描的常见代币中：无持仓）")
    return eth, found


def main():
    if len(sys.argv) > 1:
        addr = sys.argv[1]
    else:
        addr = ""
        for ln in Path(".env").read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s.lower().startswith("account_address") or (s and not s.startswith("#") and "0x" in s and "KEY" not in s.upper()):
                k, _, v = s.partition("=")
                if "address" in k.lower():
                    addr = v.strip().strip('"').strip("'")
                    break
    addr = addr.strip()
    if not addr.startswith("0x") or len(addr) != 42:
        print("用法: python tools/scan_wallet.py 0x地址")
        return 1
    print(f"扫描地址: {addr}   （全程只读，不发送任何交易）")
    eth_px = 2480.0  # 参考价，仅用于粗略估值
    results = {}
    for chain in CHAINS:
        r = scan(chain, addr, eth_px)
        if r:
            results[chain] = r
    print(f"\n{'=' * 70}\n汇总（按 ETH ≈ ${eth_px:,.0f} 估算）\n{'=' * 70}")
    total = 0.0
    for chain, (eth, toks) in results.items():
        v = eth * eth_px
        print(f"  {chain:<10} 原生币 ≈ ${v:,.2f}" + (f"  + {len(toks)} 种代币" if toks else ""))
        total += v
    print(f"  合计（仅原生币）≈ ${total:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

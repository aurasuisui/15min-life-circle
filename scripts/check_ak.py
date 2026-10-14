#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度地图 AK 验证脚本 —— 零第三方依赖（只用 Python 标准库）

用法:
    python scripts/check_ak.py            # 跑全部检查
    python scripts/check_ak.py --sn-only  # 只跑 SN 签名自检（不需要 AK）

它会：
  0) 用官方测试向量验证 SN 签名实现（不需要真实 AK）
  1) 地理编码    上海市浦东新区海科路1650号   (任务书 FR-1 验收原文)
  2) 坐标转换    WGS84 -> BD09
  3) 步行批量算路 1 起点 x 2 终点            (等时圈核心接口)
  4) 地点检索    单关键字基线
  5) 地点检索    多关键字并集  <-- D1 闸门 G1
     跑完请去控制台「我的额度」看 地点检索 当天消耗 +1 还是 +3，
     这个数字决定 POI 策略（见 docs/SOLUTION.md 2.2）
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---- Windows 控制台编码修复：不做这一步，GBK 控制台遇到 emoji 会直接崩 ----
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)   # 切到 UTF-8 代码页
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:                                       # noqa: BLE001
        pass
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                       # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
BASE = "https://api.map.baidu.com"

# 控制台配额（个人开发者，2025-10 版《开发者权益》）
QUOTA = {"geocoding": 5000, "place": 100, "routematrix": 5000, "geoconv": 5000}

# ---------------------------------------------------------------- SN 签名

NON_RESERVED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~"
)


def bd_encode(value: str) -> str:
    """按百度官方「编码说明」字符表白名单编码。

    官方原文：非保留字 a-z A-Z 0-9 - _ . ~ 不编码，凡是不在下表中的字符都需要编码。
    Python 的 quote(safe='') 行为与该表完全一致（空格 -> %20）。
    """
    return urllib.parse.quote(str(value), safe="", encoding="utf-8")


def make_sn(path: str, params: dict, sk: str) -> str:
    """SN = MD5( urlencode( path + "?" + encoded_query + sk ) )，32 位小写。

    三步（官方《附录 · sn 计算算法》）：
      1. 参数按 URL 中出现的顺序，对每个 value 做 UTF-8 编码，拼成 k=v&k=v
      2. wholeStr = path + "?" + queryString + sk        <- sk 直接拼，无分隔符
      3. sn = md5( encode(wholeStr) )                    <- 整串【再编码一次】
    """
    query = "&".join(f"{k}={bd_encode(v)}" for k, v in params.items())
    whole = f"{path}?{query}{sk}"
    return hashlib.md5(bd_encode(whole).encode("utf-8")).hexdigest()


def selftest_sn() -> bool:
    """官方测试向量：ak=yourak, sk=yoursk, /geocoder/v2/?address=百度大厦&output=json&ak=yourak
    期望 sn = 7de5a22212ffaa9e326444c75a58f9a0
    """
    params = {"address": "百度大厦", "output": "json", "ak": "yourak"}
    got = make_sn("/geocoder/v2/", params, "yoursk")
    want = "7de5a22212ffaa9e326444c75a58f9a0"
    ok = got == want
    print(f"  path     : /geocoder/v2/")
    print(f"  query    : {bd_encode('address')}=... (params in URL order)")
    print(f"  got sn   : {got}")
    print(f"  want sn  : {want}")
    print(f"  result   : {'PASS' if ok else 'FAIL'}")
    if not ok:
        print("  >> 签名实现有误。最常见原因：漏掉第 3 步「整串再编码一次」。")
    return ok


# ---------------------------------------------------------------- HTTP

def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for k in ("BAIDU_MAP_SERVER_AK", "BAIDU_MAP_SERVER_SK",
              "VITE_BAIDU_MAP_BROWSER_AK"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def call(path: str, params: dict, ak: str, sk: str | None) -> dict:
    params = dict(params)
    params.setdefault("output", "json")
    # ak 必须进 query —— 之前漏了这一行，导致所有请求都无 AK，
    # 百度一律回 status=101「AK参数不存在」，让人误以为是 AK 没生效。
    params["ak"] = ak
    if sk:
        params["timestamp"] = str(int(__import__("time").time()))
        # sn 必须最后计算、且放在最后；签名覆盖的 params 必须与 URL 中完全一致（含 ak）
        params["sn"] = make_sn(path, params, sk)
    qs = "&".join(f"{k}={bd_encode(v)}" for k, v in params.items())
    url = f"{BASE}{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "15min-life-circle/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"status": -1, "message": f"HTTP {e.code}"}
    except Exception as e:                                   # noqa: BLE001
        return {"status": -1, "message": str(e)}


def explain(status: int, message: str, has_sk: bool = False) -> str:
    """把百度状态码翻译成人话（注意：不同 API 语义不一致，这里按服务看）。"""
    m = (message or "").lower()
    if status == 0:
        return "OK"
    if status in (4, 302) or "quota" in m or "超限" in (message or ""):
        return "❌ 当日配额已耗尽 -> 不要重试，切离线快照"
    if status == 401:
        return "⚠️ QPS 并发超限 -> 降速重试"
    if status in (5, 101, 200, 201, 202) or "ak" in m:
        return "❌ AK 非法/不存在 -> 检查 .env 是否填对、AK 是否被删"
    if status == 210:
        return "❌ IP 白名单校验失败 -> 该 AK 应该改用 SN 签名"
    if status == 211:
        if not has_sk:
            return ("❌ 该 AK 使用【SN 签名】校验，但 .env 未配置 SK -> "
                    "去控制台「应用管理」复制该 AK 的私钥，填入 BAIDU_MAP_SERVER_SK")
        return "❌ SN 校验失败 -> 检查签名实现（尤其「整串再编码」）与 timestamp"
    if status in (203, 240):
        return ("❌ AK 类型/服务错误 -> 两种可能：(a) 用了浏览器端 AK 调服务端接口；"
                "(b) 回控制台确认该 AK 已勾选本接口对应的服务")
    if status == 2:
        return "⚠️ 参数错误（注意 routematrix 用 2 表示参数错，语义与 place 不同）"
    return f"⚠️ 未预期状态码 {status}: {message}"


# ---------------------------------------------------------------- checks

def check_geocoding(ak: str, sk: str | None) -> None:
    print("\n[1] 地理编码  上海市浦东新区海科路1650号   (配额 -1)")
    r = call("/geocoding/v3/", {
        "address": "上海市浦东新区海科路1650号",
        "city": "上海市",
    }, ak, sk)
    print(f"    raw: {json.dumps(r, ensure_ascii=False)[:220]}")
    if r.get("status") == 0:
        loc = r["result"]["location"]
        print(f"    ✅ BD09 = {loc['lat']:.6f}, {loc['lng']:.6f} "
              f"(confidence={r['result'].get('confidence')}, "
              f"level={r['result'].get('level')})")
    else:
        print("    " + explain(r.get("status", -1), r.get("message", ""), has_sk=bool(sk)))


def check_geoconv(ak: str, sk: str | None) -> None:
    print("\n[2] 坐标转换  WGS84(121.6,31.2) -> BD09   (配额 -1)")
    r = call("/geoconv/v1/", {
        "coords": "121.6,31.2", "from": 1, "to": 5,   # 1=WGS84  5=BD09LL
    }, ak, sk)
    print(f"    raw: {json.dumps(r, ensure_ascii=False)[:220]}")
    if r.get("status") == 0:
        p = r["result"][0]
        print(f"    ✅ BD09 = {p['y']:.6f}, {p['x']:.6f}")
    else:
        print("    " + explain(r.get("status", -1), r.get("message", ""), has_sk=bool(sk)))


def check_routematrix(ak: str, sk: str | None) -> None:
    print("\n[3] 步行批量算路  1 起点 x 2 终点 = 2 条路线   (配额 -2)")
    r = call("/routematrix/v2/walking", {
        "origins": "31.204000,121.610000",
        "destinations": "31.206000,121.612000|31.208000,121.615000",
        "ret_straight_dist": 1,       # 同一次配额内同时返回直线距离 -> 零成本标定绕行系数
    }, ak, sk)
    print(f"    raw: {json.dumps(r, ensure_ascii=False)[:260]}")
    if r.get("status") == 0:
        for i, item in enumerate(r.get("result", [])):
            dur = item.get("duration", {}).get("value", 0)
            dis = item.get("distance", {}).get("value", 0)
            flag = "  <-- value=0 表示【不可达】，不是 0 秒！" if dur == 0 else ""
            print(f"    终点{i + 1}: {dis:>6} m / {dur:>5} s{flag}")
        print("    ✅ 接口可用（同时拿到了直线距离，可用于标定 circuity factor）")
    else:
        print("    " + explain(r.get("status", -1), r.get("message", ""), has_sk=bool(sk)))
        if r.get("status") == 211 and sk:
            # 本机实测：routematrix 在【服务未开通】时也返回 211（而不是 240）。
            # 判别方法：同一 AK 下 geocoding / place / directionlite 都正常，
            # 只有 routematrix 报 211 —— 那就是「批量算路」没勾选。
            print("    ⚠️  若同 AK 的其他接口都正常、只有这里 211 -> 大概率是【批量算路】未开通。")
            print("        控制台 → 应用管理 → 该 AK → 编辑 → 勾选「批量算路」→ 保存，再重跑本脚本。")


def check_place(ak: str, sk: str | None) -> None:
    print("\n[4] 地点检索  单关键字 query=药店   (配额 -1)")
    r = call("/place/v3/around", {
        "query": "药店", "location": "31.204,121.610",
        "radius": 1500, "radius_limit": "true", "page_size": 20, "scope": 1,
    }, ak, sk)
    if r.get("status") == 0:
        n = len(r.get("results", []))
        print(f"    ✅ 返回 {n} 条；样例："
              f"{r['results'][0]['name'] if n else '(空)'}")
    else:
        print(f"    raw: {json.dumps(r, ensure_ascii=False)[:220]}")

        print("    " + explain(r.get("status", -1), r.get("message", ""), has_sk=bool(sk)))


def check_place_multi(ak: str, sk: str | None) -> None:
    print("\n[5] 地点检索  多关键字并集 query=药店$综合医院$诊所   "
          "(配额 -1 还是 -3 ??  ← D1 闸门 G1)")
    r = call("/place/v3/around", {
        "query": "药店$综合医院$诊所", "location": "31.204,121.610",
        "radius": 1500, "radius_limit": "true", "page_size": 20, "scope": 1,
    }, ak, sk)
    if r.get("status") == 0:
        results = r.get("results", [])
        print(f"    ✅ 返回 {len(results)} 条"
              f"（3 关键字 x page_size 20 = 理论上限 60）")
        for item in results[:3]:
            print(f"       - {item.get('name')}  [{item.get('tag', '')}]")
        print("\n    >>> 现在去控制台看「我的额度 / 用量统计」：")
        print("        地点检索今天比刚才多消耗了 1 次  -> 按【请求】计费，")
        print("           8 类设施可压到 6 次/社区（同质大类分组），方案按 §2.2 走")
        print("        多消耗了 3 次                    -> 按【关键字】计费，")
        print("           执行 §5.4 回退方案（底库 + 快照 + 降采样）")
    else:
        print(f"    raw: {json.dumps(r, ensure_ascii=False)[:220]}")
        print("    " + explain(r.get("status", -1), r.get("message", ""), has_sk=bool(sk)))


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn-only", action="store_true",
                    help="只跑 SN 签名自检（不需要 AK）")
    args = ap.parse_args()

    print("=" * 68)
    print("百度地图 AK 验证  ·  15min-life-circle")
    print("=" * 68)

    print("\n[0] SN 签名自检（官方测试向量，不需要 AK，配额 -0）")
    if not selftest_sn():
        return 2
    if args.sn_only:
        return 0

    env = load_env()
    ak = env.get("BAIDU_MAP_SERVER_AK", "")
    sk = env.get("BAIDU_MAP_SERVER_SK", "")
    browser_ak = env.get("VITE_BAIDU_MAP_BROWSER_AK", "")

    print("\n--- 配置检查 ---")
    print(f"  .env            : {'存在' if ENV_FILE.exists() else '不存在 (先 cp .env.example .env)'}")
    print(f"  浏览器端 AK     : {'已填 ' + browser_ak[:6] + '...' if browser_ak else '未填'}")
    print(f"  服务端 AK       : {'已填 ' + ak[:6] + '...' if ak else '未填'}")
    print(f"  服务端 SK       : {'已填 ****' if sk else '未填 (SN 签名必需)'}")

    if not ak or ak.startswith("请填入"):
        print("\n❌ 服务端 AK 未配置，跳过联网检查。")
        print("   请先按 docs/AK申请指南.md 申请，再把 AK/SK 写进 .env")
        return 1

    use_sn = bool(sk) and not sk.startswith("请填入")
    if not use_sn:
        print("\n⚠️  未配置 SK -> 按「IP 白名单」方式调用（无 timestamp/sn）")
        print("    评审方换机器将无法复现，强烈建议改用 SN 签名。")

    print("\n--- 联网检查（会消耗真实配额，本次合计约 6~8 次）---")
    check_geocoding(ak, sk if use_sn else None)
    check_geoconv(ak, sk if use_sn else None)
    check_routematrix(ak, sk if use_sn else None)
    check_place(ak, sk if use_sn else None)
    check_place_multi(ak, sk if use_sn else None)

    print("\n" + "=" * 68)
    print("完成。别忘了把第 [5] 步的结论回填到 docs/SOLUTION.md §2.2。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())

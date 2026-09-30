# -*- coding: utf-8 -*-
"""
diamond_compound_lite.py —— 武器合成挑选（干净版）
=================================================
只做两件事：
  1. 读取你自己的武器池/材料库存，按 7.2 规则自动挑选最优合成配对（纯本地计算，不联网）；
  2. 仅当你显式加 --do N 时，才用你自己账号的 token 调用游戏【正常】合成接口
     POST /api/weapon/compound_weapon 执行合成（消耗你的真实武器与材料，由服务器校验）。

本脚本【不包含】任何流量篡改能力：
  - 无 mitmproxy / 中间人代理 / 请求拦截
  - 无修改请求体（count -1→0 之类）
  - 无伪造成功响应、无封号拦截
  - 无钻石/矿石凭空产出、无无限抽取、无抓取他人凭证

用法：
  python diamond_compound_lite.py                     # 读取武器池，预览 Top 配对
  python diamond_compound_lite.py --data 路径.json     # 指定武器数据文件
  python diamond_compound_lite.py --preview 20         # 展示前 20 条配对
  python diamond_compound_lite.py --do 1               # 执行第 1 条配对的合成（真实消耗资源）
  python diamond_compound_lite.py --do 1 --matter none # 合成时不加材料
  python diamond_compound_lite.py --do 1 --matter 7004 # 指定材料 id

武器数据文件格式（weaponget_weapons.json）：
  {
    "code": 0,
    "data": { "weapons": [ {"id":"<实例id>","weapon_id":101,"price":45000,"skill":[...],"gems":[...],
                            "combat_attr":[...],"damage_attr":[...],"extra_attr":[...],
                            "attack":...,"life":...}, ... ] },
    "materials": [ {"id":7004,"count":12}, ... ]
  }
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
import queue
import re
import ssl
import sys
import threading
import time
import urllib.request
import base64
import gzip
import urllib.error


# 控制台 UTF-8 输出（打包后 exe 在中文系统 cmd 下不乱码）
def _setup_console_utf8():
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


_setup_console_utf8()

# 运行目录：打包后 = exe 所在目录；源码运行 = 本文件目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 服务器 / 签名（来自游戏正常客户端请求方式）────────────────────
PROD_HOST = "prod-build-adventure.lanfeitech.com"
API_BASE = f"https://{PROD_HOST}"
DEFAULT_APPID = "UyieUJa8"
DEFAULT_SIGNKEY = "a84720a35e80bccff8948fbe5163ff96"

# 已消耗"材料刀"标记价：游戏 get_weapons 不删除被合成消耗的武器，
# 而是把价格改成 3114（FODDER_RESULT_PRICE）。配对/加载/获取时统一过滤。
FODDER_RESULT_PRICE = 3114


def is_usable_weapon(w):
    """过滤两类非正常武器：
    - 已消耗材料刀（价格 == 3114，游戏标记）
    - 低级西瓜刀（weapon_id=134 且 quality<6，早期刷的垃圾材料刀，游戏不计入武器数量）
    """
    if not isinstance(w, dict) or not w.get("id"):
        return False
    try:
        if int(w.get("price", 0)) == FODDER_RESULT_PRICE:
            return False
    except Exception:
        pass
    try:
        if int(w.get("weapon_id", 0)) == 134 and int(w.get("quality", 0)) < 6:
            return False
    except Exception:
        pass
    return True


def dedup_weapons(weapons):
    """每种武器（weapon_id 模板）只保留一把：优先价值高，同价值再比质量。
    服务器 get_weapons 可能返回重复实例（如多把西瓜刀 weapon_id=134），按模板去重。"""
    best = {}
    for w in weapons or []:
        if not isinstance(w, dict) or not w.get("id"):
            continue
        try:
            wid = int(w.get("weapon_id", 0))
        except Exception:
            continue
        try:
            q = int(w.get("quality", 0))
        except Exception:
            q = 0
        try:
            p = int(w.get("price", 0))
        except Exception:
            p = 0
        cur = best.get(wid)
        if cur is None:
            best[wid] = w
            continue
        try:
            cq = int(cur.get("quality", 0))
        except Exception:
            cq = 0
        try:
            cp = int(cur.get("price", 0))
        except Exception:
            cp = 0
        if (p, q) > (cp, cq):
            best[wid] = w
    return list(best.values())


# ── 材料词典：id → 技能/宝石代码 ───────────────────────────────────
MATTER_GEMS = {7001: [1, 2, 3, 4], 7002: [7, 8, 9, 10], 7003: [11, 12, 13, 14]}
MATTER_SKILLS = {
    7004: [2, 40, 50, 55], 7005: [15, 28, 42, 47], 7006: [7, 19, 25, 62],
    7007: [6, 8, 32, 35], 7008: [5, 9, 13, 24], 7009: [17, 31, 54, 59],
    7010: [23, 36, 38, 43], 7011: [3, 22, 34, 48], 7012: [16, 27, 39, 49],
    7013: [11, 29, 51, 61], 7014: [4, 21, 37, 44], 7015: [20, 33, 45, 53],
    7016: [14, 30, 46, 58], 7017: [26, 41, 52, 56], 7018: [1, 10, 12, 18],
}
MATTER_NAME = {
    7001: "大地碎片", 7002: "奥术水晶", 7003: "骸骨面具", 7004: "强能拳套",
    7005: "珍贵水晶", 7006: "凛冬之息", 7007: "原初之火", 7008: "古树藤蔓",
    7009: "暗影荆棘", 7010: "剧毒孢子", 7011: "兽主鬓毛", 7012: "精金斧刃",
    7013: "生机之种", 7014: "暗影骨骸", 7015: "灼热触手", 7016: "收割镰刀",
    7017: "零度臻冰", 7018: "兽骨长枪",
}


def matter_codes(mid):
    mid = int(mid)
    if mid in MATTER_SKILLS:
        return list(MATTER_SKILLS[mid]), "skill"
    if mid in MATTER_GEMS:
        return list(MATTER_GEMS[mid]), "gem"
    return [], ""


# ── 属性 ID 对照（展示用） ────────────────────────────────────────
attr_name_map = {
    1001: "命中", 1002: "暴击", 1003: "闪避", 1004: "格挡",
    1005: "冰霜伤害", 1006: "火焰伤害", 1007: "闪电伤害", 1008: "暗影伤害",
    1009: "暗影抗性", 1010: "火焰抗性", 1011: "闪电抗性", 1012: "冰霜抗性",
}

# ── 武器名称表（ID -> 名称）───────────────────────────────────────
weapon_name_text = """灵巧短匕1 礼仪短剑2 平衡战矛3 锋利猎刀4 英勇战斧5 黑铁重剑6 破魔刃7 荣耀三叉戟8 保护者阔斧9 波纹之剑10 战斗先驱11 无畏战刃12 龙鳞13 毁灭者14 混乱终结者15 复仇之心16 勇气之虹17 黄昏之回响18 裂地之锋19 风暴大剑20 飓风战斧21 炽炎蒸腾长剑22 守护者之匕23 雄狮咆哮24 命运回响之刃25 雷霆保卫者26 暗影侵袭27 震慑之斧28 德萨格锐恩29 夜魔之刺30 冰霜碎裂之力31 塔兰格尔32 焕彩破碎者33 泰坦巨戟34 科林修斯35 破碎之刃36 骨刃37 冥主38 碎骨39 赛博坦之斧40 国王之剑41 宙斯42 璀璨星辰43 马桶撅44 翡翠匕首45 光剑46 破阵斧47 逐风者之剑101 万千真理之剑102 亚瑟王之剑103 剑豪之剑104 船长的剑105 斩魄之刃106 匕首107 兄弟会之剑108 亡灵战斧109 莫格莱尼之剑110 火尖枪111 诺德战斧112 方天画戟113 苦无114 拳剑115 撬棍116 像素剑117 绯樱太刀118 动力剑119 动力长矛120 链锯剑121 契约之剑122 斩舰刀123 克劳德的剑124 封弊者之剑125 战神之斧126 魔剑127 屠龙刀128 傲慢之斧129 路牌130 杀猪刀131 美工刀132 铁锹133 西瓜刀134 暗月大剑135 林克之剑136 狂战士之斧137 无尽之剑138 破败之剑139 忍者之剑140 天顶之剑141 缠流之剪142 钢翼巨剑143 猎人太刀144 命运之枪145 黎明守卫146 水晶剑147 断刀148 龙胆亮银枪149 盘古开天斧150 旦丁之剑151 剑风勇士之剑152 巨阙153 地狱之斧154 地狱之刃155 轩辕剑156 湛卢157 维度长枪158 玉髓巨剑159 青玉长枪160 猎犬之斧161 赤霄剑162 萝卜刀163 1000t大锤164 裁决大剑165 青龙偃月刀166 倚天剑167 玄铁重剑168 干将169 莫邪170 破阵霸王枪171 海神三叉戟172 永恒之枪173 金蛇剑174 九霄弑神枪175 雷霆之矛176 阿瑞斯之剑177 精金战斧178 暴风战斧179 越王勾践剑180 逆刀刃181 铁碎牙182 大夏龙雀刀183 青虹剑184 龙泉剑185 七星刀186 古锭刀187 昆吾188 新亭侯189 不死斩190 望舒剑191 羲和剑192 心影193 声波刀194 天火大剑195 咸鱼剑196 枪刃197 龙牙198 虎翼199 犬神200 等离子太刀201 大葱202 鸣鸿刀203 暗裔刀204 鲨鱼匕首205 新月玫瑰206 时之刃207 血饮狂刃208 火龙大剑209 吉他斧210 猎头者211 天元刀212 翔龙战斧213 末路大剑214 稻光215 赤练太刀216 能量剑217 青牙218 影之锋刃219 星蚀220 龙裔之刃221 月卫之剑222 光明使者223 灼炎狂刃224 霜渊太刀225 霓虹斩魄226 沧澜圣锋227 幻心之刺228 骸骨镰刃229 魔瞳剑230 幽影猎刀231 自然使者232 黑曜重剑233 冰蓝长锋234 曜金匕首235 赤发236 鎏金弧月237 毒蝰238 云秀刀239 寒铁刺240 塑形之刃241 寒骨242 盖娅之怒243 血色荆棘244 暗喻阔刃245 冥骨之刺246 敌法者247 达摩克利斯剑248 蔓生破坏者249 生命之息250 悬锋圣剑251 自然教义252 海列屈拉253 灰黯之手254 钢铁终结者255 霓虹周波刀256 索伦之眼257 原能战斧258 惊澜259 霜巨人战矛260 利维坦之牙261 月华战戟262 星之彩263 碎颅者264 血蔷薇265 鸿运金锤266 席卡战刃267 高周波战矛268 逐波269 重渊270 阿西莫夫271 崩岳272 应星273 霓虹曲调274 异星猎手275 指令原型276 碧玺刃277 驱暗者278 异界合金剑279 斩马斧280 玄武281 皓然剑282 原能佩剑283 异教徒284 岚285 暗金破坏者286 凯西特矛287 纳努特大剑288 建木刃289 证道七星290 妖异291 魔金刺292 异魔之眼293 无归294 烈日裁决者295 极星296 热能刃297 逆熵298 机械教条299 雅辛特拉300 狮之心301 神铸战斧302 指令重塑303 血狂短刃304 光明战刃305 青金匕首306 日轮审判者307 夜幽细剑308 酋长短匕309 弗尔的短剑310 提伯丁之怒311 寄生毁灭者312 八岐之眼313 帝皇战刃314 荣耀破坏者315 端木316 天将巨剑317 雷鸣切318 喋血狂刃319"""
weapon_id_name_map = {}
for _line in weapon_name_text.split():
    _line = _line.strip()
    if not _line:
        continue
    nums = re.findall(r"\d+", _line)
    if not nums:
        continue
    _wid = int(nums[-1])
    _name = re.sub(r"\s*\d+$", "", _line).strip()
    weapon_id_name_map[_wid] = _name


def get_weapon_name(wid):
    try:
        wid_i = int(wid)
    except Exception:
        wid_i = 0
    return weapon_id_name_map.get(wid_i, f"未知武器({wid})")


# ── 合成配置（可在 config.json 的 compound 节覆盖） ────────────────
DEFAULT_COMPOUND_CONFIG = {
    "min_weapon_value": 30000,
    "max_weapon_value": 52000,
    "min_traits": 0,
    "show_top_n": 10,
    "skill_gem_overlap_threshold": 0.2,
    "skill_gem_min_combined": 0,
    "min_total_tags": 0,
    "unique_weapon_only": 1,
    "matter_overflow_threshold": 0,
}
DEFAULT_COMPOUND_TEMPLATE = {
    "extra_num": 4, "damage_num": 4, "combat_num": 4, "skill_num": 4,
    "slot_num": 4, "attack_num": 2100, "life_num": 2100, "ronghe_num": 4,
}


def _config_path(base_dir=None):
    return os.path.join(base_dir or BASE_DIR, "config.json")


def load_compound_config(base_dir=None):
    cfg = copy.deepcopy(DEFAULT_COMPOUND_CONFIG)
    path = _config_path(base_dir)
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return cfg
    if not isinstance(raw, dict):
        return cfg
    src = raw.get("compound") if isinstance(raw.get("compound"), dict) else raw
    for k, dv in DEFAULT_COMPOUND_CONFIG.items():
        if k in src:
            try:
                cfg[k] = type(dv)(src[k]) if not isinstance(dv, bool) else bool(src[k])
            except Exception:
                pass
    return cfg


# ── 通用工具 ──────────────────────────────────────────────────────
def _as_id_list(val):
    if val is None:
        return []
    if isinstance(val, dict):
        return [int(k) for k in val if str(k).isdigit()]
    if isinstance(val, (list, tuple)):
        out = []
        for x in val:
            try:
                out.append(int(x))
            except Exception:
                pass
        return out
    try:
        return [int(val)]
    except Exception:
        return []


def weapon_skills(w):
    return _as_id_list(w.get("skill") if isinstance(w, dict) else None)


def weapon_gems(w):
    return _as_id_list(w.get("gems") if isinstance(w, dict) else None)


def weapon_price(w):
    if not isinstance(w, dict):
        return 0
    for k in ("price", "score"):
        if w.get(k) is not None:
            try:
                return int(w[k])
            except Exception:
                pass
    return 0


def trait_count(w):
    return len(set(weapon_skills(w))) + len(set(weapon_gems(w)))


def get_attr_id_and_values(weapon, key):
    arr = weapon.get(key, []) if isinstance(weapon, dict) else []
    ids, value_dict = set(), {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        aid = item.get("id")
        if aid is None:
            continue
        try:
            aid_i = int(aid)
        except Exception:
            continue
        ids.add(aid_i)
        try:
            value_dict[aid_i] = int(item.get("value", 0))
        except Exception:
            value_dict[aid_i] = 0
    return ids, value_dict


# ── 数据加载（只读本地武器池/材料，不做任何网络拦截） ───────────────
def default_data_path(base_dir=None):
    sess = os.environ.get("DIAMOND_SESSION_DIR")
    if sess:
        return os.path.join(sess, "weaponget_weapons.json")
    return os.path.join(base_dir or BASE_DIR, "weaponget_weapons.json")


def load_data(path=None):
    target = path or default_data_path()
    if target.endswith("wid.json"):
        d = os.path.dirname(os.path.abspath(target))
        target = os.path.join(d, "weaponget_weapons.json")
    if not os.path.isfile(target):
        return {"weapons": [], "materials": [], "path": target, "exists": False}
    try:
        # utf-8-sig：兼容带 BOM 的 JSON（PowerShell/部分导出工具会写 BOM）
        with open(target, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)
    except Exception as e:
        return {"weapons": [], "materials": [], "path": target, "exists": True, "error": str(e)}
    data = payload.get("data") if isinstance(payload, dict) else {}
    weapons = list(data.get("weapons") or []) if isinstance(data, dict) else []
    weapons = [w for w in weapons if is_usable_weapon(w)]
    weapons = dedup_weapons(weapons)
    materials = list(payload.get("materials") or []) if isinstance(payload, dict) else []
    return {"weapons": weapons, "materials": materials, "path": target, "exists": True}


def save_pool(path, weapons, materials=None, source=None):
    """把武器池与材料库存写回本地数据文件（统一格式）。"""
    target = path or default_data_path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        payload = {
            "code": 0,
            "data": {"weapons": weapons or []},
            "materials": materials or [],
            "source": source or "local",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(target, "w", encoding="utf-8-sig") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def load_token():
    """读取你自己账号的 token（credentials.json 或 --token）。仅用于正常 API 调用。"""
    cred = os.path.join(BASE_DIR, "credentials.json")
    if os.path.isfile(cred):
        try:
            with open(cred, "r", encoding="utf-8") as f:
                obj = json.load(f)
            tok = obj.get("token") or ""
            uid = obj.get("uid") or ""
            if tok and not tok.lower().startswith("bearer "):
                tok = f"Bearer {tok}"
            return tok, uid
        except Exception:
            pass
    return "", ""


# ── 7.2 配对筛选（纯本地计算） ─────────────────────────────────────
def find_compound_pairs(weapons, config=None):
    if config is None:
        config = DEFAULT_COMPOUND_CONFIG
    min_v = int(config.get("min_weapon_value") or 0)
    max_v = int(config.get("max_weapon_value") or 0)
    min_traits = int(config.get("min_traits") or 0)
    skill_gem_min_combined = int(config.get("skill_gem_min_combined") or 0)
    overlap_th = float(config.get("skill_gem_overlap_threshold") or 0)
    min_total_tags = int(config.get("min_total_tags") or 0)
    unique_only = bool(int(config.get("unique_weapon_only") or 0))

    weapons = dedup_weapons(weapons or [])
    filtered = []
    for w in weapons:
        if not isinstance(w, dict) or not w.get("id"):
            continue
        if not is_usable_weapon(w):  # 过滤已消耗材料刀（价格=3114）
            continue
        price = weapon_price(w)
        if min_v != 0 and price < min_v:
            continue
        if max_v != 0 and price >= max_v:
            continue
        if trait_count(w) < min_traits:
            continue
        filtered.append(w)
    n = len(filtered)
    if n < 2:
        return []

    valid_pairs = []
    for i in range(n):
        wa = filtered[i]
        s_a, g_a = weapon_skills(wa), weapon_gems(wa)
        set_s_a, set_g_a = set(s_a), set(g_a)
        set_c_a, val_c_a = get_attr_id_and_values(wa, "combat_attr")
        set_d_a, val_d_a = get_attr_id_and_values(wa, "damage_attr")
        set_e_a, val_e_a = get_attr_id_and_values(wa, "extra_attr")
        life_a, attack_a = wa.get("life", 0), wa.get("attack", 0)
        price_a = weapon_price(wa)

        for j in range(i + 1, n):
            wb = filtered[j]
            s_b, g_b = weapon_skills(wb), weapon_gems(wb)
            set_s_b, set_g_b = set(s_b), set(g_b)
            set_c_b, val_c_b = get_attr_id_and_values(wb, "combat_attr")
            set_d_b, val_d_b = get_attr_id_and_values(wb, "damage_attr")
            set_e_b, val_e_b = get_attr_id_and_values(wb, "extra_attr")
            life_b, attack_b = wb.get("life", 0), wb.get("attack", 0)
            price_b = weapon_price(wb)

            overlap_s_cnt = len(set_s_a & set_s_b)
            overlap_g_cnt = len(set_g_a & set_g_b)
            sum_s = len(s_a) + len(s_b)
            sum_g = len(g_a) + len(g_b)
            overlap_s_ratio = (overlap_s_cnt / sum_s) if sum_s > 0 else 0.0
            overlap_g_ratio = (overlap_g_cnt / sum_g) if sum_g > 0 else 0.0
            if overlap_s_ratio > overlap_th or overlap_g_ratio > overlap_th:
                continue

            total_skill = len(set_s_a | set_s_b)
            total_gem = len(set_g_a | set_g_b)
            total_combat = len(set_c_a | set_c_b)
            total_damage = len(set_d_a | set_d_b)
            total_extra = len(set_e_a | set_e_b)
            combined_sg = total_skill + total_gem
            if skill_gem_min_combined > 0 and combined_sg < skill_gem_min_combined:
                continue
            total_all_tags = total_skill + total_gem + total_combat + total_damage + 2
            if min_total_tags > 0 and total_all_tags < min_total_tags:
                continue

            if price_a >= price_b:
                first, second = wa, wb
            else:
                first, second = wb, wa

            combat_range = {}
            for aid in (set_c_a | set_c_b):
                va = val_c_a.get(aid, 0); vb = val_c_b.get(aid, 0)
                combat_range[attr_name_map.get(aid, str(aid))] = f"{min(va, vb)}~{max(va, vb)}"
            damage_range = {}
            for aid in (set_d_a | set_d_b):
                va = val_d_a.get(aid, 0); vb = val_d_b.get(aid, 0)
                damage_range[attr_name_map.get(aid, str(aid))] = f"{min(va, vb)}~{max(va, vb)}"
            extra_range = {}
            for aid in (set_e_a | set_e_b):
                va = val_e_a.get(aid, 0); vb = val_e_b.get(aid, 0)
                extra_range[attr_name_map.get(aid, str(aid))] = f"{min(va, vb)}~{max(va, vb)}"

            valid_pairs.append({
                "weapon_a_uuid": first.get("id"), "weapon_a_id": first.get("weapon_id"),
                "weapon_a_name": get_weapon_name(first.get("weapon_id")),
                "weapon_a_obj": first,
                "weapon_b_uuid": second.get("id"), "weapon_b_id": second.get("weapon_id"),
                "weapon_b_name": get_weapon_name(second.get("weapon_id")),
                "weapon_b_obj": second,
                "price_a": weapon_price(first), "price_b": weapon_price(second),
                "a_skill": weapon_skills(first), "b_skill": weapon_skills(second),
                "a_gems": weapon_gems(first), "b_gems": weapon_gems(second),
                "merged_skill": sorted(set_s_a | set_s_b),
                "merged_gems": sorted(set_g_a | set_g_b),
                "merged_combat": sorted(set_c_a | set_c_b),
                "merged_damage": sorted(set_d_a | set_d_b),
                "merged_extra": sorted(set_e_a | set_e_b),
                "base_attack_range": f"{min(attack_a, attack_b)}~{max(attack_a, attack_b)}",
                "base_life_range": f"{min(life_a, life_b)}~{max(life_a, life_b)}",
                "overlap_skill_ratio": round(overlap_s_ratio, 4),
                "overlap_gem_ratio": round(overlap_g_ratio, 4),
                "overlap_skill_count": overlap_s_cnt,
                "overlap_gem_count": overlap_g_cnt,
                "a_combat_ids": list(set_c_a if first is wa else set_c_b),
                "b_combat_ids": list(set_c_b if first is wa else set_c_a),
                "a_damage_ids": list(set_d_a if first is wa else set_d_b),
                "b_damage_ids": list(set_d_b if first is wa else set_d_a),
                "a_extra_ids": list(set_e_a if first is wa else set_e_b),
                "b_extra_ids": list(set_e_b if first is wa else set_e_a),
                "combat_attr_preview": combat_range,
                "damage_attr_preview": damage_range,
                "extra_attr_preview": extra_range,
                "total_skill": total_skill, "total_gem": total_gem,
                "skill_gem_sum": combined_sg,
                "total_combat": total_combat, "total_damage": total_damage,
                "total_extra": total_extra, "total_all_tags": total_all_tags,
            })

    valid_pairs.sort(key=lambda x: x["total_all_tags"], reverse=True)
    if unique_only:
        used, deduped = set(), []
        for pair in valid_pairs:
            a, b = pair.get("weapon_a_uuid"), pair.get("weapon_b_uuid")
            if a in used or b in used:
                continue
            deduped.append(pair)
            used.add(a)
            used.add(b)
        valid_pairs = deduped
    return valid_pairs


# ── 材料选择 + 溢出计算 ───────────────────────────────────────────
def compute_matter_overflow(mid, merged_skills, merged_gems):
    codes, kind = matter_codes(mid)
    mid_i = int(mid)
    if kind == "skill":
        target_set = set(merged_skills or [])
    elif kind == "gem":
        target_set = set(merged_gems or [])
    else:
        return {"matter_id": mid_i, "kind": kind, "codes": codes,
                "overlap_codes": [], "overflow_count": 0, "useful_count": 0,
                "new_codes": [], "label": matter_label(mid_i)}
    overlap_codes = sorted(set(codes) & target_set)
    new_codes = sorted(set(codes) - target_set)
    overlap_count, useful_count = len(overlap_codes), len(new_codes)
    return {
        "matter_id": mid_i, "kind": kind, "codes": codes,
        "overlap_codes": overlap_codes, "overflow_count": overlap_count,
        "useful_count": useful_count, "new_codes": new_codes,
        "label": matter_label(mid_i),
    }


def list_matter_options(skills, gems, materials=None, config=None):
    if config is None:
        config = DEFAULT_COMPOUND_CONFIG
    overflow_th = int(config.get("matter_overflow_threshold") or 0)
    inventory = {}
    for m in materials or []:
        try:
            inventory[int(m.get("id"))] = int(m.get("count", 0))
        except Exception:
            pass
    results = []
    for mid in list(MATTER_SKILLS.keys()) + list(MATTER_GEMS.keys()):
        info = compute_matter_overflow(mid, skills, gems)
        if overflow_th > 0 and info["overflow_count"] > overflow_th:
            continue
        cnt = inventory.get(int(mid), 0)
        info["count"] = cnt
        info["label"] = f"{mid} {MATTER_NAME.get(int(mid),'')} 数量×{cnt} codes={info['codes']} 重叠(+{info['overflow_count']}) 新增{info['useful_count']}"
        results.append(info)
    results.sort(key=lambda r: (0 if r["count"] > 0 else 1, -r["useful_count"], r["overflow_count"]))
    return results


def pick_matter_id(skills, gems, materials=None, config=None):
    """选一个玩家确实拥有的材料；无库存信息或玩家没有可用材料时返回 None（不加材料合成）。"""
    opts = list_matter_options(skills, gems, materials=materials, config=config)
    if materials is not None:
        pool = [o for o in opts if o["count"] > 0]
    else:
        # 无库存信息时不再按"有用性"盲选，避免带上玩家没有的材料导致 11012
        pool = []
    if not pool:
        return None, 0, "", None
    best = pool[0]
    return best["matter_id"], best["useful_count"], best["kind"], best


def matter_label(matter_id, count=None, overflow=None):
    if not matter_id:
        return "(不加材料)"
    name = MATTER_NAME.get(int(matter_id), "")
    codes_list = MATTER_SKILLS.get(int(matter_id)) or MATTER_GEMS.get(int(matter_id)) or []
    tail = []
    if count is not None:
        tail.append(f"数量×{count}")
    if overflow is not None and overflow > 0:
        tail.append(f"(+{overflow})")
    suffix = (" " + " ".join(tail)) if tail else ""
    return f"{matter_id} {name} codes={codes_list}{suffix}"


# ── 合成请求体构建（与游戏正常客户端一致） ─────────────────────────
def merge_level_tb(w1, w2, level=30):
    skills = sorted(set(weapon_skills(w1)) | set(weapon_skills(w2)))
    gems = sorted(set(weapon_gems(w1)) | set(weapon_gems(w2)))
    return {
        "skills": [{"id": i, "level": int(level)} for i in skills],
        "gems": [{"id": i, "level": int(level)} for i in gems],
    }, skills, gems


def build_compound_body(uid, first, second, use_matter=True, template=None,
                        matter_id=None, materials=None, config=None):
    tpl = dict(DEFAULT_COMPOUND_TEMPLATE)
    if template:
        tpl.update(template)
    level_tb, skills, gems = merge_level_tb(first, second)
    body = {
        "uid": uid,
        "first_wid": str(first.get("id")),
        "second_wid": str(second.get("id")),
        **tpl,
        "level_tb": level_tb,
    }
    score, kind = 0, ""
    mid, info = matter_id, None
    if use_matter:
        if mid is None:
            mid, score, kind, info = pick_matter_id(skills, gems, materials=materials, config=config)
        else:
            info = compute_matter_overflow(mid, skills, gems)
            cnt = 0
            for m in materials or []:
                if str(m.get("id")) == str(mid):
                    try:
                        cnt = int(m.get("count", 0))
                    except Exception:
                        pass
                    break
            info["count"] = cnt
            score, kind = info["useful_count"], info["kind"]
        if mid is not None:
            body["matter_id"] = int(mid)
    meta = {
        "skills": skills, "gems": gems,
        "matter_id": body.get("matter_id"), "matter_score": score, "matter_kind": kind,
        "matter_label": matter_label(body.get("matter_id"),
                                     count=(info or {}).get("count"),
                                     overflow=(info or {}).get("overflow_count")),
    }
    return body, meta


# ── 正常合成 API 调用（携带你自己账号 token，含签名） ──────────────
# 网络方式与游戏正常客户端一致：IP 直连 + Host 头 + 跳过证书校验
TARGET_IP = "47.117.153.230"


def _normalize_endpoint(endpoint, default="/"):
    """把用户输入的接口归一化成纯路径，兼容多种写法：
       /api/weapon/get_weapons
       https://host/api/weapon/get_weapons
       host/api/weapon/get_weapons
    """
    ep = (endpoint or "").strip()
    if not ep:
        return default
    if "://" in ep:
        ep = ep.split("://", 1)[1]
    if not ep.startswith("/"):
        idx = ep.find("/")
        if idx >= 0:
            ep = ep[idx:]
        else:
            ep = "/"
    return ep


# 全局 HTTPS 连接池：连接保持打开复用（keep-alive），本地端口占用稳定，避免高并发打爆
# Windows 动态端口（WinError 10048）。池内连接由队列互斥，同一连接不会并发请求。
_CONN_POOL_SIZE = 32
_CONN_POOL = queue.Queue(maxsize=_CONN_POOL_SIZE)
for _ in range(_CONN_POOL_SIZE):
    _CONN_POOL.put(None)
_conn_ctx = None


def _get_conn(timeout):
    """从全局连接池取一个连接；空闲连接保持打开复用（不再每请求新建，端口不耗尽）。"""
    global _conn_ctx
    if _conn_ctx is None:
        _conn_ctx = ssl.create_default_context()
        _conn_ctx.check_hostname = False
        _conn_ctx.verify_mode = ssl.CERT_NONE
    try:
        conn = _CONN_POOL.get(timeout=max(3, min(timeout or 10, 15)))
    except Exception:
        conn = None
    if conn is None or getattr(conn, "sock", None) is None:
        conn = http.client.HTTPSConnection(TARGET_IP, 443, context=_conn_ctx, timeout=timeout)
    return conn


def _release_conn(conn):
    """请求完成：连接放回池（保持打开，供下次复用）。池满则关闭丢弃，不阻塞。"""
    try:
        _CONN_POOL.put(conn, timeout=1)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def _drop_conn(conn):
    """连接失效：关闭并放回空占位（池内重建）。"""
    try:
        conn.close()
    except Exception:
        pass
    try:
        _CONN_POOL.put(None, timeout=1)
    except Exception:
        pass


def _post_once(conn, path, headers, body_bytes):
    """单次请求；返回 (status, raw)。连接失败抛异常。"""
    conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.putheader("Content-Length", str(len(body_bytes)))
    conn.endheaders(body_bytes)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", errors="replace")
    return resp.status, raw

def api_post(token, path, body, appid=None, signkey=None, timeout=20, time_override=None):
    appid = appid or DEFAULT_APPID
    signkey = signkey or DEFAULT_SIGNKEY
    path = _normalize_endpoint(path, default="/")
    ts = time_override if time_override is not None else str(int(time.time()))
    body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    sign = hashlib.md5(
        f"appid={appid}&body={body_str}&signkey={signkey}&time={ts}&".encode("utf-8")
    ).hexdigest()
    if token and not token.lstrip().startswith("Bearer "):
        token = "Bearer " + token.lstrip()
    headers = {
        "Content-Type": "application/json",
        "Authorization": token if token else "",
        "time": ts,
        "sign": sign,
        "Host": PROD_HOST,
        "version": "1",
        "User-Agent": "okhttp/4.9.0",
    }
    body_bytes = body_str.encode("utf-8")
    conn = None
    try:
        conn = _get_conn(timeout)
        try:
            status, raw = _post_once(conn, path, headers, body_bytes)
        except (ConnectionError, OSError, http.client.HTTPException, ssl.SSLError) as e:
            # 连接失效：重建并重试一次
            _drop_conn(conn)
            conn = _get_conn(timeout)
            status, raw = _post_once(conn, path, headers, body_bytes)
        _release_conn(conn)
        conn = None
    except Exception as e:
        if conn is not None:
            _drop_conn(conn)
        return {"error": str(e)}
    if status >= 400:
        return {"http_error": status, "body": raw}
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}




def compound_weapon(token, uid, first, second, use_matter=True, matter_id=None,
                    materials=None, config=None, log=None):
    log = log or (lambda m: None)
    body, meta = build_compound_body(
        uid, first, second, use_matter=use_matter, matter_id=matter_id,
        materials=materials, config=config,
    )
    log(f"合成请求: first={body['first_wid']} second={body['second_wid']} "
        f"skills={meta['skills']} gems={meta['gems']} matter={meta['matter_label']}")
    if not token:
        log("缺少 token（未提供 --token，且 credentials.json 无有效凭证）")
        return None, body, meta
    try:
        resp = api_post(token, "/api/weapon/compound_weapon", body)
    except urllib.error.HTTPError as e:
        resp = {"http_error": e.code, "body": e.read().decode("utf-8", errors="replace")[:500]}
    except Exception as e:
        resp = {"error": str(e)}
    if isinstance(resp, dict) and resp.get("code") == 0:
        info = ((resp.get("data") or {}).get("first_info") or {})
        if isinstance(info, dict) and info:
            log(f"合成成功[主武器保留]: id={info.get('id')} 价值={weapon_price(info)} "
                f"宝石={weapon_gems(info)} 技能={weapon_skills(info)}")
        else:
            log(f"合成成功: {resp}")
    else:
        log(f"合成未成功: {resp}")
    return resp, body, meta


# ── 获取武器池（只读，保存到本地数据文件） ─────────────────────────
DEFAULT_WEAPON_API = "/api/weapon/get_weapons"


def fetch_weapons(token, uid, endpoint=None, path=None, timeout=20):
    """
    用你自己账号的 token 只读调用武器列表接口，把返回的武器保存到本地数据文件，
    并同步从 get_user_info 拉取合成材料库存（70xx 且 count>0）。
    返回 (weapons, resp)。失败时 weapons 为 None。
    endpoint 可在界面修改（默认 /api/weapon/get_weapons，兼容完整网址写法）。
    """
    endpoint = _normalize_endpoint(endpoint, default=DEFAULT_WEAPON_API)
    if not token:
        return None, {"error": "缺少 token"}
    try:
        resp = api_post(token, endpoint, {"uid": uid}, timeout=timeout)
    except urllib.error.HTTPError as e:
        resp = {"http_error": e.code, "body": e.read().decode("utf-8", errors="replace")[:500]}
    except Exception as e:
        resp = {"error": str(e)}
    if not isinstance(resp, dict) or resp.get("code") != 0:
        return None, resp
    data = resp.get("data") or {}
    weapons = data.get("weapons") or []
    if not isinstance(weapons, list):
        return None, resp
    # 过滤已消耗材料刀（价格=3114），避免消耗过的武器反复出现/参与配对
    weapons = [w for w in weapons if is_usable_weapon(w)]
    # 每种武器模板只保留一把（服务器可能返回重复实例）
    weapons = dedup_weapons(weapons)

    # 同步拉取材料库存（与原版 fetch_weapons_api 一致：get_user_info 的 70xx 且 count>0）
    materials = []
    try:
        g = api_post(token, "/api/user/get_user_info", {"uid": uid}, timeout=timeout)
        if isinstance(g, dict) and g.get("code") == 0:
            for gg in (g.get("data") or {}).get("user_goods", []):
                try:
                    mid = int(gg.get("id"))
                    cnt = int(gg.get("count") or 0)
                except Exception:
                    continue
                if 7001 <= mid <= 7018 and cnt > 0:
                    materials.append({"id": mid, "count": cnt})
    except Exception:
        pass

    target = path or default_data_path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        payload = {
            "code": 0,
            "data": {"weapons": weapons},
            "materials": materials,
            "source": endpoint,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(target, "w", encoding="utf-8-sig") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return weapons, {"save_error": str(e)}
    return weapons, resp


# ── 打造专用池（3114 材料刀也纳入打造） ───────────────────────────
def forge_pool(weapons):
    """打造专用池：全部武器保留（含西瓜刀），同类型重复实例只保留价值最高的一把。"""
    items = []
    for w in weapons or []:
        if not isinstance(w, dict) or not w.get("id"):
            continue
        items.append(w)
    return dedup_weapons(items)


def fetch_forge_pool(token, uid, endpoint=None, timeout=20):
    """只读拉取武器池并返回打造专用池（含 3114 材料刀，已按类型去重）。
    不写数据文件——合成/价值分布等仍使用 fetch_weapons 的纯净池。"""
    endpoint = _normalize_endpoint(endpoint, default=DEFAULT_WEAPON_API)
    if not token:
        return None, {"error": "缺少 token"}
    try:
        resp = api_post(token, endpoint, {"uid": uid}, timeout=timeout)
    except urllib.error.HTTPError as e:
        resp = {"http_error": e.code, "body": e.read().decode("utf-8", errors="replace")[:500]}
    except Exception as e:
        resp = {"error": str(e)}
    if not isinstance(resp, dict) or resp.get("code") != 0:
        return None, resp
    data = resp.get("data") or {}
    weapons = data.get("weapons") or []
    if not isinstance(weapons, list):
        return None, resp
    return forge_pool(weapons), resp


# ── 展示 ─────────────────────────────────────────────────────────
def format_pair(i, p, config=None, materials=None):
    mat = ""
    try:
        opts = list_matter_options(p["merged_skill"], p["merged_gems"],
                                   materials=materials, config=config)
        best = next((o for o in opts if o["count"] > 0), opts[0] if opts else None)
        mat = f" 推荐材料: {best['label']}" if best else " 无推荐材料"
    except Exception:
        pass
    return (f"[{i}] {p['weapon_a_name']}({p['weapon_a_uuid']},价值{p['price_a']}) + "
            f"{p['weapon_b_name']}({p['weapon_b_uuid']},价值{p['price_b']})  |  "
            f"合并词条总数={p['total_all_tags']} (技能{p['total_skill']}+宝石{p['total_gem']})  "
            f"重叠率 技能{p['overlap_skill_ratio']}/宝石{p['overlap_gem_ratio']}  "
            f"基础攻击{p['base_attack_range']} 生命{p['base_life_range']}{mat}")


# ── 自动打造 / 补矿（正常游戏接口，消耗真实资源，服务器校验） ─────────
DEFAULT_FORGE_TEMPLATE = {
    "accuracy": 100,
    "extra_num": 4,
    "damage_num": 4,
    "combat_num": 4,
    "skill_num": 4,
    "slot_num": 4,
    "attack_num": 2700,
    "life_num": 2700,
    "ronghe_num": 4,
}


def parse_forge_template(text):
    """解析用户填写的打造模板 JSON。失败返回 (None, 错误信息)。"""
    if not text or not text.strip():
        return dict(DEFAULT_FORGE_TEMPLATE), None
    try:
        obj = json.loads(text.strip())
    except Exception as e:
        return None, "模板 JSON 解析失败: %s" % e
    if not isinstance(obj, dict):
        return None, "模板必须是 JSON 对象"
    out = {}
    for k, v in obj.items():
        try:
            out[k] = int(v)
        except Exception:
            out[k] = v
    return out, None


def fetch_resources(token, uid, timeout=20):
    """只读查询玩家资源（矿/钻石）。返回 data dict；失败返回 None。"""
    try:
        resp = api_post(token, "/api/user/get_user_resource", {"uid": uid}, timeout=timeout)
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode("utf-8", errors="replace")[:300]}
    except Exception as e:
        return {"error": str(e)}
    if not isinstance(resp, dict) or resp.get("code") != 0:
        return resp
    return resp.get("data") or {}


def fetch_sdkid(token, uid, timeout=20):
    """从 get_user_info 读取 sdkid（补矿/钻石兑换请求需要）。"""
    try:
        resp = api_post(token, "/api/user/get_user_info", {"uid": uid}, timeout=timeout)
    except Exception:
        return ""
    if isinstance(resp, dict) and resp.get("code") == 0:
        return str((resp.get("data") or {}).get("sdkid") or "")
    return ""


def pick_lowest_weapons(weapons, n, include_fodder=False):
    """从武器池中挑价值最低的 n 把（过滤不可用武器，保留完整记录）。
    include_fodder=True 时保留 3114 材料刀与全部西瓜刀（打造专用）。"""
    usable = []
    for w in (weapons or []):
        if is_usable_weapon(w):
            usable.append(w)
        elif include_fodder:
            try:
                if int(w.get("price", 0)) == FODDER_RESULT_PRICE or int(w.get("weapon_id", 0)) == 134:
                    usable.append(w)
            except Exception:
                pass
    usable.sort(key=weapon_price)
    return usable[:n]


def fetch_timestamp(token, uid, timeout=15):
    """获取服务器时间戳（客户端用它作为 iid），失败返回 None。"""
    try:
        resp = api_post(token, "/api/api/timestamp", {"uid": uid}, timeout=timeout)
    except Exception:
        return None
    try:
        if isinstance(resp, dict) and resp.get("code") == 0:
            return int(resp["data"]["timestamp"])
    except Exception:
        pass
    return None


_last_iid_sec = None
_last_iid_sub = 0


def _to_ms(ts):
    """转 13 位毫秒时间戳（已是毫秒则原样返回）。"""
    if ts >= 100000000000:
        return ts
    return ts * 1000


def _to_sec(ts):
    """转 10 位秒时间戳。"""
    if ts >= 100000000000:
        return ts // 1000
    return ts


_iid_lock = threading.Lock()



_clock_off = None
_clock_off_valid = False


def forge_calibrate(token, uid):
    """档位一：校准服务器时钟偏移（请求前后中点消 RTT），供轮内复用。
    成功返回 True；重试 5 次仍失败返回 False（调用方应停止循环，避免 16010）。"""
    global _clock_off, _clock_off_valid
    for _ in range(5):
        t0 = time.time()
        ts = fetch_timestamp(token, uid)
        t1 = time.time()
        if ts is not None:
            with _iid_lock:
                _clock_off = _to_sec(ts) - (t0 + t1) / 2.0
                _clock_off_valid = True
            return True
        time.sleep(0.3)
    with _iid_lock:
        _clock_off_valid = False
    return False




# ── 16010 修复核心：iid 单调大偏移空间（越过手机客户端时钟历史）──────
# 服务器对每个账号校验"锻造 iid 必须大于上次"（防重放）。
# 真实手机锻造 iid ≈ (服务器秒+64)*1000+ms，工具此前用"服务器秒"空间导致回退 16010。
# 修复：iid = 服务器秒*1000 + 700001（+700s 偏移），并全局单调递增。
_IID_BIAS = 700001
_last_forge_iid = 0
_last_ore_iid = 0
_sec_cached = None
_sec_cached_at = 0.0


def forge_sec(token, uid):
    """获取服务器当前秒（缓存 2 秒，保证同批扣矿/锻造同秒且不减慢）。失败返回 None。"""
    global _sec_cached, _sec_cached_at
    now = time.time()
    with _iid_lock:
        if _sec_cached is not None and now - _sec_cached_at < 2.0:
            return _sec_cached
    ts = fetch_timestamp(token, uid)
    if ts is None:
        return None
    sec = _to_sec(ts)
    with _iid_lock:
        _sec_cached = sec
        _sec_cached_at = now
    return sec


def forge_iid_mono(sec, bias=None):
    """锻造用单调 iid：服务器秒*1000 + 大偏移，全局递增（线程锁）。返回 (iid, hdr)。
    bias 可选：16010 自适应时升级偏移。"""
    global _last_forge_iid, _IID_BIAS
    with _iid_lock:
        if bias is not None:
            _IID_BIAS = bias
        iid = max(sec * 1000 + _IID_BIAS, _last_forge_iid + 1)
        _last_forge_iid = iid
        return str(iid), str(sec)


def deduct_ore_batch(token, uid, sec, count):
    """批量扣矿（真实客户端锻造前置动作；iid 单调，秒*1000+递增）。返回 code。"""
    global _last_ore_iid
    with _iid_lock:
        iid = max(sec * 1000 + 1, _last_ore_iid + 1)
        _last_ore_iid = iid
    rb = {"uid": uid,
          "update_resource": [{"goods_key": "cast_ore", "type": 0,
                               "count": int(count), "goods_id": 3, "time": 1}],
          "iid": str(iid)}
    try:
        r = api_post(token, "/api/user/update_user_resource", rb, time_override=str(sec))
    except Exception:
        return None
    return r.get("code") if isinstance(r, dict) else None

def forge_iid(token, uid):
    """档位一：轮内 iid —— 实时本地时间 + 已校准偏移 = 服务器毫秒（含亚秒相位），
    同秒递增去重。必须在 forge_calibrate 成功后调用；未校准则退回实时校准。"""
    global _last_iid_sec, _last_iid_sub
    with _iid_lock:
        if not _clock_off_valid:
            return _fresh_iid(token, uid)
        srv_f = time.time() + _clock_off
        srv = int(srv_f)
        iid_ms = int(srv_f * 1000)
        if srv == _last_iid_sec:
            _last_iid_sub += 1
        else:
            _last_iid_sec = srv
            _last_iid_sub = 1
        return str(iid_ms + _last_iid_sub), str(srv)


def _fresh_iid(token, uid):
    """13 位毫秒 iid（取自服务器时间），同一秒内递增去重。
    返回 (iid, 服务器秒)，时间戳接口失败时退回本地毫秒。
    线程锁保护全局递增状态，支持并发打造。"""
    global _last_iid_sec, _last_iid_sub
    ts = None
    for _ in range(6):
        ts = fetch_timestamp(token, uid)
        if ts is not None:
            break
        time.sleep(0.2)
    with _iid_lock:
        if ts is not None:
            if ts == _last_iid_sec:
                _last_iid_sub += 1
            else:
                _last_iid_sec = ts
                _last_iid_sub = 1
            return str(_to_ms(ts) + _last_iid_sub), str(_to_sec(ts))
        ms = int(time.time() * 1000)
        return str(ms), str(int(time.time()))


def batch_forge(token, uid, weapon_ids, template=None, timeout=30, iid_pair=None):
    """批量打造：用模板重铸指定 weapon_id 的武器（消耗真实矿石，服务器校验）。
    返回 (resp, body)。iid_pair=(iid,hdr) 可选：传入则复用该时间戳（档位一：每轮校准一次）。"""
    template = dict(DEFAULT_FORGE_TEMPLATE if not template else template)
    weapons = []
    for wid in weapon_ids:
        entry = {"weapon_id": int(wid)}
        entry.update(template)
        weapons.append(entry)
    if iid_pair:
        iid, hdr = iid_pair
    else:
        iid, hdr = _fresh_iid(token, uid)
    body = {
        "iid": iid,
        "uid": uid,
        "is_check": 1,
        "is_forge": 1,
        "weapons": weapons,
    }
    try:
        resp = api_post(token, "/api/weapon/batch_forge", body, timeout=timeout, time_override=hdr)
    except urllib.error.HTTPError as e:
        resp = {"http_error": e.code, "body": e.read().decode("utf-8", errors="replace")[:300]}
    except Exception as e:
        resp = {"error": str(e)}
    return resp, body



# ── 存档只读查询（服务器存档解密展示，不做任何修改/上传）────────────
try:
    from Crypto.Cipher import AES as _AES
    from Crypto.Util.Padding import unpad as _unpad
    _HAS_CRYPTO = True
except Exception:
    _HAS_CRYPTO = False


def _xor_val(v):
    """混淆存储 {value,fade,xor} → 真实值 = fade ^ xor。"""
    if isinstance(v, dict):
        fade = v.get("fade")
        x = v.get("xor")
        if fade is not None and x is not None:
            try:
                return int(fade) ^ int(x)
            except Exception:
                pass
        return v.get("value", v)
    return v


def fetch_archive(token, uid, timeout=20):
    """拉取服务器存档（只读）。返回 (存档5字段dict, 原始响应)。"""
    body = {"uid": uid, "archives": [{"tb_name": "archive"}], "version": 1}
    resp = api_post(token, "/api/archive/get", body, timeout=timeout)
    if not isinstance(resp, dict) or resp.get("code") != 0:
        return None, resp
    arch = None
    for a in resp.get("data", {}).get("archives", []):
        if isinstance(a, dict) and a.get("tb_name") == "archive" and isinstance(a.get("data"), dict):
            arch = a
            break
    if not arch:
        return None, resp
    return arch.get("data") or {}, resp


def decode_archive_ud(b64, uid):
    """解密 UserData：base64 → AES-ECB(uid) → unpad → base64 → gzip → JSON。"""
    if not _HAS_CRYPTO:
        return None
    try:
        ct = base64.b64decode(b64)
        pt = _unpad(_AES.new(uid.encode("utf-8"), _AES.MODE_ECB).decrypt(ct), 16)
        gz = base64.b64decode(pt)
        return json.loads(gzip.decompress(gz).decode("utf-8"))
    except Exception:
        return None


_ARCH_LABELS = [
    ("Nickname", "昵称"),
    ("Coin", "金币"),
    ("Ore", "矿石"),
    ("SavingPotOre", "存矿罐矿石"),
    ("ItemThreeOre", "三倍矿卡"),
    ("Diamond", "钻石"),
    ("ForgeCount", "总打造次数"),
    ("NewForgeCount", "新打造次数"),
    ("ForgeCountByHand", "手搓次数"),
    ("ForgeMergeCount", "融合次数"),
    ("NewPlayerForgeCount", "新手打造次数"),
    ("CastOreCount", "铸造矿石次数"),
    ("CastDiamondCount", "铸钻次数"),
    ("DailyCustomForgeMaterialCount", "每日自定义锻造材料"),
    ("SkillUpCount", "技能升级次数"),
    ("CurrentCheckPoint", "当前关卡"),
    ("MaxCheckPoint", "最高关卡"),
    ("UpdateOreTime", "矿石更新时间"),
    ("UpdateOreForgeCount", "更新时打造次数"),
]


def _pad_pkcs7(data):
    """AES-ECB 补齐到 16 字节倍数（PKCS7）。"""
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def encode_xor_val(value):
    """写入混淆值 {fade,xor}：真实值 = fade ^ xor。随机 fade 防止固定值被检测。"""
    import random
    v = int(value)
    fade = random.randint(1, 0xFFFFFF)
    return {"fade": fade, "xor": fade ^ v, "value": v}


def encrypt_archive_ud(ud, uid):
    """加密 UserData（decode_archive_ud 的逆向）：
    JSON → gzip → base64 → AES-ECB(uid) → base64。失败返回 None。"""
    if not _HAS_CRYPTO:
        return None
    try:
        raw = json.dumps(ud, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        gz = base64.b64encode(gzip.compress(raw))
        ct = _AES.new(uid.encode("utf-8"), _AES.MODE_ECB).encrypt(_pad_pkcs7(gz))
        return base64.b64encode(ct).decode("utf-8")
    except Exception:
        return None


def upload_archive(token, uid, arch_data, field_key, new_value, timeout=20):
    """修改存档字段并加密上传。arch_data 为 archive/get 返回的 data（含 UserData 等）。
    返回 (成功bool, 消息)。"""
    if not isinstance(arch_data, dict) or not arch_data.get("UserData"):
        return False, "存档数据缺失"
    ud = decode_archive_ud(arch_data["UserData"], uid)
    if ud is None:
        return False, "UserData 解密失败"
    old = ud.get(field_key)
    ud[field_key] = encode_xor_val(new_value)
    enc = encrypt_archive_ud(ud, uid)
    if not enc:
        return False, "UserData 加密失败"
    data = dict(arch_data)
    data["UserData"] = enc
    body = {"uid": uid, "archives": [{"tb_name": "archive", "data": data}]}
    resp = api_post(token, "/api/archive/upload", body, timeout=timeout)
    if not isinstance(resp, dict):
        return False, str(resp)
    if resp.get("code") == 0:
        return True, "上传成功（原值 %s -> %s）" % (old, new_value)
    return False, resp.get("msg") or resp.get("error") or json.dumps(resp, ensure_ascii=False)


def archive_summary(ud):
    """把解密后的 UserData 整理成有序 [(字段名key, 标签, 值), ...]（含无值字段），
    及每武器打造次数明细。ForgeCountArray 兼容 dict / list[{'key','value'}] 结构。"""
    out = []
    for key, label in _ARCH_LABELS:
        v = ud.get(key)
        if v is None:
            out.append((key, label, None))
            continue
        if key == "UpdateOreTime":
            try:
                t = int(_xor_val(v))
                val = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
            except Exception:
                val = str(v)
        else:
            val = _xor_val(v)
        out.append((key, label, val))
    fca = ud.get("ForgeCountArray")
    detail = {}

    def _collect(k, v):
        if k is not None:
            detail[str(k)] = _xor_val(v)

    if isinstance(fca, dict):
        for wid, v in fca.items():
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        _collect(item.get("key"), item.get("value"))
            else:
                _collect(wid, v)
    elif isinstance(fca, list):
        for item in fca:
            if isinstance(item, dict):
                _collect(item.get("key"), item.get("value"))
    return out, detail


def diamond_convert(token, uid, sdkid, count=1, timeout=20):
    """钻石兑换矿石（补矿）：消耗真实钻石，服务器扣钻记账。
    返回 (resp, body)。"""
    iid, hdr = _fresh_iid(token, uid)
    body = {
        "uid": uid,
        "sdkid": sdkid,
        "iid": iid,
        "update_goods_tb": [{"id": 1, "count": -int(count), "from": "DiamondConvert"}],
        "ver": 0,
    }
    try:
        resp = api_post(token, "/api/user/update_user_goods", body, timeout=timeout, time_override=hdr)
    except urllib.error.HTTPError as e:
        resp = {"http_error": e.code, "body": e.read().decode("utf-8", errors="replace")[:300]}
    except Exception as e:
        resp = {"error": str(e)}
    return resp, body




def main(argv):
    args = list(argv)
    data_path = None
    preview_n = None
    do_idx = None
    matter = None
    token_override = None
    use_matter = True

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--data" and i + 1 < len(args):
            data_path = args[i + 1]; i += 2
        elif a == "--preview" and i + 1 < len(args):
            preview_n = int(args[i + 1]); i += 2
        elif a == "--do" and i + 1 < len(args):
            do_idx = int(args[i + 1]); i += 2
        elif a == "--matter" and i + 1 < len(args):
            v = args[i + 1]
            if v.lower() == "none":
                use_matter = False
            elif v.lower() == "auto":
                use_matter = True
            else:
                matter = int(v)
            i += 2
        elif a == "--token" and i + 1 < len(args):
            token_override = args[i + 1]; i += 2
        else:
            i += 1

    config = load_compound_config()
    data = load_data(data_path)
    if not data["exists"]:
        print(f"未找到武器数据文件: {data['path']}")
        print("请先导出你账号的 get_weapons 响应，按格式放入该 JSON（见脚本头注释）。")
        return 1
    if data.get("error"):
        print(f"武器数据文件解析失败: {data['error']}")
        print(f"文件路径: {data['path']}")
        return 1

    weapons = data["weapons"]
    materials = data["materials"]
    print(f"武器池: {len(weapons)} 把武器；材料: {len(materials)} 条库存；数据文件: {data['path']}")
    if not weapons:
        print("武器池为空。")
        return 1

    pairs = find_compound_pairs(weapons, config)
    if not pairs:
        print("没有满足条件的配对（可能武器数量不足 2，或都被价格/词条/重叠率过滤）。")
        return 1

    n = preview_n or int(config.get("show_top_n") or 10)
    print(f"\n===== 最优合成配对 Top {min(n, len(pairs))} =====")
    for idx, p in enumerate(pairs[:n], start=1):
        print(format_pair(idx, p, config, materials=materials))
        print(f"     ├ 合成后技能: {p['merged_skill']}")
        print(f"     └ 合成后宝石: {p['merged_gems']}")

    if do_idx is None:
        print("\n(仅预览。如需执行合成，请加 --do <序号> —— 会真实消耗你的武器和材料)")
        return 0

    if not (1 <= do_idx <= len(pairs)):
        print(f"错误: --do {do_idx} 超出配对范围 1~{len(pairs)}")
        return 1

    token, uid = load_token()
    if token_override:
        token = token_override
    if token and not uid:
        uid = ""
    print(f"\n===== 执行合成 第 {do_idx} 条 =====")
    pair = pairs[do_idx - 1]
    first, second = pair["weapon_a_obj"], pair["weapon_b_obj"]
    resp, body, meta = compound_weapon(
        token, uid, first, second,
        use_matter=use_matter, matter_id=matter,
        materials=materials if materials else None,
        config=config, log=lambda m: print("  " + m),
    )
    if isinstance(resp, dict) and resp.get("code") == 0:
        print("合成完成。建议把返回的 first_info 同步回你的武器数据文件（data.weapons upsert）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

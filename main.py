# -*- coding: utf-8 -*-
"""自动挂机合成（手机版 v1）——Kivy 跨平台客户端。
复用 diamond_compound_lite.py 业务库：凭证签名、拉池、打造、合成。
页面：凭证 / 武器池查询 / 自动挂机（含日志）。
"""
import json
import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import diamond_compound_lite as dcl

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
from kivy.uix.textinput import TextInput

CRED_FILE = os.path.join(BASE, "mobile_creds.json")
IMPROVE_FILE = os.path.join(BASE, "improve_history.json")

_CREDS = {}
_UI_LOCK = threading.Lock()
_UIQ = []


# ---------------------------------------------------------------- 工具

def _load_creds():
    global _CREDS
    try:
        if os.path.exists(CRED_FILE):
            _CREDS = json.load(open(CRED_FILE, encoding="utf-8"))
    except Exception:
        _CREDS = {}
    return _CREDS


def _save_creds():
    try:
        json.dump(_CREDS, open(CRED_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_improve(uid):
    try:
        if os.path.exists(IMPROVE_FILE):
            data = json.load(open(IMPROVE_FILE, encoding="utf-8"))
        else:
            data = {}
        if data and not any(isinstance(v, dict) and "best_value" in v for v in data.values()):
            return {}
        return data.get(uid, {})
    except Exception:
        return {}


def _save_improve(uid, records):
    try:
        data = {}
        if os.path.exists(IMPROVE_FILE):
            try:
                data = json.load(open(IMPROVE_FILE, encoding="utf-8"))
            except Exception:
                data = {}
        if not any(isinstance(v, dict) and "best_value" in v for v in data.values()):
            data = {}
        data[uid] = records
        json.dump(data, open(IMPROVE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass


def _record_improve(uid, records, wid, old_price, new_price, source):
    try:
        old_price = int(old_price or 0)
        new_price = int(new_price or 0)
        wid = int(wid)
    except Exception:
        return
    if new_price <= old_price:
        return
    rec = records.get(str(wid))
    if rec is None:
        records[str(wid)] = {
            "name": dcl.get_weapon_name(wid),
            "initial_value": old_price,
            "best_value": new_price,
            "source": source,
            "best_time": time.strftime("%m-%d %H:%M"),
        }
    elif new_price > rec.get("best_value", 0):
        rec["best_value"] = new_price
        rec["best_time"] = time.strftime("%m-%d %H:%M")
        rec["source"] = source


# ---------------------------------------------------------------- 日志区

class LogView(ScrollView):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.box = BoxLayout(orientation="vertical", size_hint_y=None)
        self.box.bind(minimum_height=self.box.setter("height"))
        self.add_widget(self.box)
        self._lines = []

    def append(self, text):
        text = str(text)
        self._lines.append(text)
        if len(self._lines) > 400:
            self._lines = self._lines[-300:]
            self.box.clear_widgets()
        lbl = Label(text=text, size_hint_y=None, height=dp(22),
                    halign="left", valign="middle", text_size=(self.width - dp(8), None))
        lbl.bind(width=lambda w, _: setattr(lbl, "text_size", (w.width - dp(8), None)))
        self.box.add_widget(lbl)
        Clock.schedule_once(lambda dt: self._scroll_end(), 0)

    def _scroll_end(self):
        try:
            self.scroll_y = 0
        except Exception:
            pass


# ---------------------------------------------------------------- 凭证页

class CredPage(BoxLayout):
    def __init__(self, app, **kw):
        super().__init__(orientation="vertical", padding=dp(10), spacing=dp(8), **kw)
        self.app = app
        lay = GridLayout(cols=1, size_hint_y=None, height=dp(230), spacing=dp(6))
        lay.add_widget(Label(text="账号 UID", size_hint_y=None, height=dp(24), halign="left"))
        self.tx_uid = TextInput(multiline=False, hint_text="粘贴 UID", size_hint_y=None, height=dp(42))
        lay.add_widget(self.tx_uid)
        lay.add_widget(Label(text="Bearer Token", size_hint_y=None, height=dp(24), halign="left"))
        self.tx_token = TextInput(multiline=True, hint_text="粘贴 Bearer token", size_hint_y=None, height=dp(90))
        lay.add_widget(self.tx_token)
        self.add_widget(lay)
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        b_save = Button(text="保存凭证")
        b_save.bind(on_release=self.save)
        b_test = Button(text="测试（拉一次池）")
        b_test.bind(on_release=self.test)
        row.add_widget(b_save)
        row.add_widget(b_test)
        self.add_widget(row)
        self.lbl = Label(text="", size_hint_y=None, height=dp(30), halign="left")
        self.add_widget(self.lbl)
        self.log = LogView()
        self.add_widget(self.log)
        self.load()

    def load(self):
        _load_creds()
        if _CREDS:
            uid = sorted(_CREDS.keys())[0]
            self.tx_uid.text = uid
            self.tx_token.text = _CREDS.get(uid, "")

    def save(self, *a):
        uid = self.tx_uid.text.strip()
        tok = self.tx_token.text.strip()
        if not uid or not tok:
            self.lbl.text = "UID 和 token 不能为空"
            return
        _CREDS[uid] = tok
        _save_creds()
        self.lbl.text = "已保存（%d 个凭证）" % len(_CREDS)

    def test(self, *a):
        uid = self.tx_uid.text.strip()
        tok = self.tx_token.text.strip()
        if not uid or not tok:
            self.lbl.text = "先填写 UID/token"
            return
        self.lbl.text = "测试中..."
        threading.Thread(target=self._do_test, args=(tok, uid), daemon=True).start()

    def _do_test(self, tok, uid):
        try:
            ws, resp = dcl.fetch_forge_pool(tok, uid, timeout=15)
            if not ws:
                self.app.ui("测试失败: %s" % (resp,))
            else:
                self.app.ui("测试成功: 拉取 %d 把武器" % len(ws))
        except Exception as e:
            self.app.ui("测试异常: %s" % e)


# ---------------------------------------------------------------- 武器池查询页

class PoolPage(BoxLayout):
    def __init__(self, app, **kw):
        super().__init__(orientation="vertical", padding=dp(10), spacing=dp(6), **kw)
        self.app = app
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        b = Button(text="拉取武器池")
        b.bind(on_release=self.fetch)
        row.add_widget(b)
        self.lbl = Label(text="未拉取", size_hint_y=None, height=dp(30), halign="left")
        self.add_widget(row)
        self.add_widget(self.lbl)
        head = GridLayout(cols=4, size_hint_y=None, height=dp(26), spacing=dp(4))
        for t in ("武器名称", "价值", "品质", "数量"):
            head.add_widget(Label(text=t, bold=True, halign="center"))
        self.add_widget(head)
        sv = ScrollView()
        self.box = GridLayout(cols=4, size_hint_y=None, spacing=dp(4))
        self.box.bind(minimum_height=self.box.setter("height"))
        sv.add_widget(self.box)
        self.add_widget(sv)

    def fetch(self, *a):
        creds = self.app.creds()
        if not creds:
            self.lbl.text = "请先在凭证页填写并保存"
            return
        self.lbl.text = "拉取中..."
        threading.Thread(target=self._do, args=creds, daemon=True).start()

    def _do(self, uid, tok):
        try:
            ws, resp = dcl.fetch_forge_pool(tok, uid, timeout=20)
            if not ws:
                self.app.ui("拉取失败: %s" % (resp,))
                return
            raw = len(ws)
            seen = {}
            dups = 0
            for w in ws:
                try:
                    wid = int(w.get("weapon_id", 0) or 0)
                except Exception:
                    continue
                if wid in seen:
                    dups += 1
                else:
                    seen[wid] = w
            self.app.ui("武器池: 原始 %d 条 → 有效 %d 把，重复 %d 条" % (raw, len(seen), dups))
            rows = []
            for wid, w in seen.items():
                try:
                    price = int(w.get("price", 0) or 0)
                except Exception:
                    price = 0
                try:
                    q = int(w.get("quality", 0) or 0)
                except Exception:
                    q = 0
                rows.append((dcl.get_weapon_name(wid), price, q, 1))
            rows.sort(key=lambda x: -x[1])
            self.app.ui_clock(lambda: self._fill(rows, len(seen), dups, raw))
        except Exception as e:
            self.app.ui("拉取异常: %s" % e)

    def _fill(self, rows, valid, dups, raw):
        self.lbl.text = "原始 %d 条 → 有效 %d 把，重复 %d 条" % (raw, valid, dups)
        self.box.clear_widgets()
        for name, price, q, cnt in rows[:200]:
            self.box.add_widget(Label(text=name, halign="left", size_hint_y=None, height=dp(24)))
            self.box.add_widget(Label(text="{:,}".format(price), halign="center", size_hint_y=None, height=dp(24)))
            self.box.add_widget(Label(text=str(q), halign="center", size_hint_y=None, height=dp(24)))
            self.box.add_widget(Label(text=str(cnt), halign="center", size_hint_y=None, height=dp(24)))
        if len(rows) > 200:
            self.box.add_widget(Label(text="...共 %d 把，仅显示前 200" % len(rows), size_hint_y=None, height=dp(24)))


# ---------------------------------------------------------------- 自动挂机页

class ForgePage(BoxLayout):
    def __init__(self, app, **kw):
        super().__init__(orientation="vertical", padding=dp(10), spacing=dp(6), **kw)
        self.app = app
        self.stop_flag = threading.Event()

        g = GridLayout(cols=2, size_hint_y=None, height=dp(250), spacing=dp(6))
        g.add_widget(Label(text="打造方式", halign="left"))
        self.sp_mode = Spinner(text="最低20把/批", values=("最低20把/批", "单把×20次"))
        g.add_widget(self.sp_mode)
        g.add_widget(Label(text="每轮最大批数", halign="left"))
        self.tx_batch = TextInput(text="50", multiline=False)
        g.add_widget(self.tx_batch)
        g.add_widget(Label(text="轮数（0=无限）", halign="left"))
        self.tx_rounds = TextInput(text="0", multiline=False)
        g.add_widget(self.tx_rounds)
        g.add_widget(Label(text="并发路数", halign="left"))
        self.tx_workers = TextInput(text="5", multiline=False)
        g.add_widget(self.tx_workers)
        g.add_widget(Label(text="阈值合成", halign="left"))
        row = BoxLayout(spacing=dp(4))
        self.cb_compound = CheckBox(active=True)
        row.add_widget(self.cb_compound)
        row.add_widget(Label(text="阈值 >", halign="left"))
        self.tx_thr = TextInput(text="52000", multiline=False, size_hint_x=0.5)
        row.add_widget(self.tx_thr)
        row.add_widget(Label(text="最多", halign="left"))
        self.tx_cmax = TextInput(text="5", multiline=False, size_hint_x=0.5)
        row.add_widget(Label(text="次", halign="left"))
        g.add_widget(row)
        self.add_widget(g)

        row2 = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        self.btn_start = Button(text="开始挂机")
        self.btn_start.bind(on_release=self.start)
        self.btn_stop = Button(text="停止")
        self.btn_stop.bind(on_release=self.stop)
        row2.add_widget(self.btn_start)
        row2.add_widget(self.btn_stop)
        self.add_widget(row2)
        self.log = LogView()
        self.add_widget(self.log)

    def params(self):
        try:
            max_batch = max(1, int(self.tx_batch.text.strip() or 50))
        except Exception:
            max_batch = 50
        try:
            rounds = int(self.tx_rounds.text.strip() or 0)
        except Exception:
            rounds = 0
        try:
            workers = max(1, min(50, int(self.tx_workers.text.strip() or 5)))
        except Exception:
            workers = 5
        try:
            thr = int(self.tx_thr.text.strip() or 52000)
        except Exception:
            thr = 52000
        try:
            cmax = max(1, int(self.tx_cmax.text.strip() or 5))
        except Exception:
            cmax = 5
        return {
            "mode": 0 if self.sp_mode.text.startswith("最低") else 1,
            "max_batch": max_batch,
            "rounds": rounds,
            "workers": workers,
            "auto_compound": bool(self.cb_compound.active),
            "threshold": thr,
            "compound_max": cmax,
        }

    def start(self, *a):
        creds = self.app.creds()
        if not creds:
            self.app.ui("请先在凭证页填写并保存凭证")
            return
        if self.stop_flag.is_set():
            self.stop_flag.clear()
        self.btn_start.disabled = True
        p = self.params()
        self.app.ui("自动挂机启动: 每批 20 把 · 每轮最多 %d 批 · 轮数 %s · 并发 %d 路 · 阈值合成=%s"
                    % (p["max_batch"],
                       "无限" if p["rounds"] == 0 else p["rounds"],
                       p["workers"], "开" if p["auto_compound"] else "关"))
        threading.Thread(target=self._worker, args=(creds[0], creds[1]), daemon=True).start()

    def stop(self, *a):
        self.stop_flag.set()
        self.app.ui("收到停止指令，完成当前请求后停止...")

    def _worker(self, uid, tok):
        p = self.params()
        rounds = 0
        compound_cnt = 0
        records = _load_improve(uid)
        template = dcl.DEFAULT_FORGE_TEMPLATE
        cfg = dcl.load_compound_config()
        prev_pool = {}
        try:
            while not self.stop_flag.is_set() and (p["rounds"] == 0 or rounds < p["rounds"]):
                rounds += 1
                self.app.ui("──── 第 %d 轮 ────" % rounds)
                t0 = time.time()
                ws, resp = dcl.fetch_forge_pool(tok, uid, timeout=20)
                if not ws:
                    self.app.ui("拉取武器池失败: %s，本轮跳过" % (resp,))
                    continue
                # 打造池（含 3114 材料刀）；合成池（过滤 fodder）
                forge_pool_all = ws
                usable = [w for w in ws if dcl.is_usable_weapon(w)]
                # 轮末提升判定（对比上一轮池）
                cur_pool = {}
                for w in ws:
                    try:
                        cur_pool[int(w.get("weapon_id", 0) or 0)] = dcl.weapon_price(w)
                    except Exception:
                        pass
                if prev_pool:
                    for wid, price in cur_pool.items():
                        old = prev_pool.get(wid)
                        if old is not None and price > old:
                            _record_improve(uid, records, wid, old, price, "打造")
                prev_pool = cur_pool
                if len(usable) < 2:
                    self.app.ui("可用武器不足，停止")
                    break
                # ---- 打造阶段 ----
                total_req = 0
                total_ok = 0
                total_fail = 0
                lock = threading.Lock()
                picked_ids = []

                def _pick_lowest(n):
                    """从打造池（含 fodder）选价值最低且未在本批选过的 n 把。"""
                    with lock:
                        got = []
                        for w in sorted(forge_pool_all, key=dcl.weapon_price):
                            if w.get("id") in picked_ids:
                                continue
                            got.append(w)
                            picked_ids.append(w.get("id"))
                            if len(got) >= n:
                                break
                        return got

                for bi in range(p["max_batch"]):
                    if self.stop_flag.is_set():
                        break
                    picked_ids = []
                    if p["mode"] == 0:
                        def _one(_i):
                            nonlocal total_req, total_ok, total_fail
                            batch_ids = [w.get("weapon_id") for w in _pick_lowest(20)]
                            if not batch_ids:
                                return
                            total_req += 1
                            rsp, body = dcl.batch_forge(tok, uid, batch_ids, template, timeout=6)
                            c = rsp.get("code") if isinstance(rsp, dict) else rsp
                            if isinstance(rsp, dict) and rsp.get("code") == 0:
                                total_ok += 1
                            else:
                                total_fail += 1
                                self.app.ui("打造未成功: %s" % (c,))

                        ths = []
                        nw = min(p["workers"], max(1, len(forge_pool_all) // 20))
                        for _i in range(nw):
                            t = threading.Thread(target=_one, args=(_i,), daemon=True)
                            t.start()
                            ths.append(t)
                        for t in ths:
                            t.join()
                    else:
                        # 单把×20：取最低 1 把，连发 20 次
                        one = _pick_lowest(1)
                        if not one:
                            break
                        wid = one[0].get("weapon_id")
                        for _k in range(20):
                            if self.stop_flag.is_set():
                                break
                            total_req += 1
                            rsp, body = dcl.batch_forge(tok, uid, [wid], template, timeout=6)
                            c = rsp.get("code") if isinstance(rsp, dict) else rsp
                            if isinstance(rsp, dict) and rsp.get("code") == 0:
                                total_ok += 1
                            else:
                                total_fail += 1
                                self.app.ui("打造未成功: %s" % (c,))
                    if self.stop_flag.is_set():
                        break
                cost = time.time() - t0
                self.app.ui("第 %d 轮完成: 请求 %d · 成功 %d · 失败 %d · 耗时 %.1f 秒"
                            % (rounds, total_req, total_ok, total_fail, cost))
                _save_improve(uid, records)
                # ---- 阈值合成 ----
                if p["auto_compound"] and p["threshold"] > 0 and len(usable) >= 4 and compound_cnt < p["compound_max"]:
                    low4 = dcl.pick_lowest_weapons(usable, 4)
                    p4 = [dcl.weapon_price(w) for w in low4]
                    avg4 = sum(p4) / 4.0
                    if avg4 > p["threshold"] and min(p4) > 30000:
                        cand = [w for w in usable if dcl.weapon_price(w) > 30000]
                        cand.sort(key=dcl.weapon_price)
                        pool50 = cand[:50]
                        pool50.sort(key=lambda w: len(w.get("skills") or []) + len(w.get("gems") or []),
                                    reverse=True)
                        used = set()
                        pairs = []
                        for i in range(len(pool50)):
                            if i in used:
                                continue
                            a = pool50[i]
                            for j in range(i + 1, len(pool50)):
                                if j in used:
                                    continue
                                b = pool50[j]
                                if not (set(a.get("skills") or []) & set(b.get("skills") or [])) \
                                        and not (set(a.get("gems") or []) & set(b.get("gems") or [])):
                                    pairs.append((a, b))
                                    used.add(i)
                                    used.add(j)
                                    break
                        self.app.ui("合成: 价值>30000 最低 %d 把 · 技能宝石不重叠 · %d 对" % (len(pool50), len(pairs)))
                        for a, b in pairs[:max(1, p["compound_max"] - compound_cnt)]:
                            rsp_c, _, _ = dcl.compound_weapon(
                                tok, uid, a, b, use_matter=True,
                                materials=None, config=cfg,
                                log=lambda m: self.app.ui("  " + m))
                            compound_cnt += 1
                            if isinstance(rsp_c, dict) and rsp_c.get("code") == 0:
                                info_c = (rsp_c.get("data") or {}).get("first_info") or {}
                                if isinstance(info_c, dict) and info_c.get("weapon_id"):
                                    try:
                                        cwid = int(info_c["weapon_id"])
                                    except Exception:
                                        cwid = None
                                    if cwid is not None:
                                        _record_improve(uid, records, cwid, dcl.weapon_price(a),
                                                        dcl.weapon_price(info_c), "合成")
                            else:
                                self.app.ui("合成未成功: %s" % (rsp_c,))
                                break
                        _save_improve(uid, records)
            if self.stop_flag.is_set():
                self.app.ui("已停止")
            else:
                self.app.ui("自动挂机结束（轮数达到设定值或可用武器不足）")
        except Exception as e:
            self.app.ui("自动挂机异常: %s" % e)
        finally:
            Clock.schedule_once(lambda dt: self._reset_btn(), 0)

    def _reset_btn(self):
        self.btn_start.disabled = False


# ---------------------------------------------------------------- 主应用

class ForgeAutoApp(App):
    def title(self):
        return "自动挂机合成"

    def build(self):
        Clock.schedule_interval(self._pump, 0.2)
        tp = TabbedPanel(tab_width=dp(120))
        self.cred_page = CredPage(self)
        self.pool_page = PoolPage(self)
        self.forge_page = ForgePage(self)
        tp.add_widget(TabbedPanelItem(text="凭证", content=self.cred_page))
        tp.add_widget(TabbedPanelItem(text="武器池", content=self.pool_page))
        tp.add_widget(TabbedPanelItem(text="自动挂机", content=self.forge_page))
        return tp

    def creds(self):
        _load_creds()
        for uid, tok in _CREDS.items():
            return uid, tok
        return None

    def ui(self, text):
        with _UI_LOCK:
            _UIQ.append(str(text))

    def ui_clock(self, fn):
        Clock.schedule_once(lambda dt: fn(), 0)

    def _pump(self, dt):
        try:
            with _UI_LOCK:
                msgs = _UIQ[:]
                del _UIQ[:]
            for m in msgs:
                for page in (self.cred_page, self.forge_page):
                    log = getattr(page, "log", None)
                    if log is not None:
                        log.append(m)
        except Exception:
            pass

    def on_stop(self):
        try:
            self.forge_page.stop_flag.set()
        except Exception:
            pass


if __name__ == "__main__":
    ForgeAutoApp().run()

"""
app.py — web interface for staff (Thai UI).

Run:   streamlit run app.py         (or double-click start_app.bat on Windows)
Then open the address it prints (http://localhost:8501).

API keys come from .env on the machine running this app; staff never see them.
"""
from __future__ import annotations
import fitz  # PyMuPDF
import streamlit as st

import common
import jobs
import offset_detect

common.load_env()
st.set_page_config(page_title="แปลง PDF ภาษาไทย", layout="wide")

STATUS_TH = {
    "queued": "รอเริ่ม", "running": "กำลังทำงาน", "submitting": "กำลังส่งงาน",
    "submitted": "ส่งแล้ว รอผล (ภายใน 24 ชม.)", "done": "เสร็จสมบูรณ์",
    "incomplete": "เสร็จบางส่วน", "stopped": "หยุดไว้", "failed": "เกิดข้อผิดพลาด",
}
MODE_NOW, MODE_BATCH = "now", "batch"


def status_th(job):
    return STATUS_TH.get(jobs.effective_status(job), job["status"])


# ---------------- sidebar ----------------

if "goto" in st.session_state:          # set by buttons; must happen before the radio
    st.session_state["nav"] = st.session_state.pop("goto")

with st.sidebar:
    st.title("แปลง PDF ภาษาไทย")
    st.caption("แปลงเอกสาร PDF ภาษาไทย (รวมตารางและภาพ) เป็นข้อความพร้อมใช้งานกับ AI")
    page = st.radio("เมนู", ["งานใหม่", "งานทั้งหมด", "ตั้งค่า"],
                    key="nav", label_visibility="collapsed")
    st.divider()
    for prov, label in (("anthropic", "Claude"), ("gemini", "Gemini"),
                        ("openai", "OpenAI")):
        st.write(("🟢 " if jobs.has_key(prov) else "⚪ ") + label)
    st.caption("🟢 = ตั้งค่าคีย์แล้ว (ผู้ดูแลระบบตั้งค่าในไฟล์ .env)")

settings = jobs.load_settings()


# ---------------- new job ----------------

def new_job_page():
    st.header("สร้างงานใหม่")

    up = st.file_uploader("1. เลือกไฟล์ PDF", type=["pdf"])
    if not up:
        st.info("เริ่มจากเลือกไฟล์ PDF ที่ต้องการแปลง")
        return
    pdf_bytes = up.getvalue()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n = len(doc)
    st.success(f"ไฟล์ **{up.name}** มีทั้งหมด {n} หน้า")

    st.subheader("2. ข้อมูลเอกสาร")
    c1, c2 = st.columns(2)
    title = c1.text_input("ชื่อเอกสาร", placeholder="เช่น คู่มือผู้ดำเนินการ...")
    publisher = c2.text_input("หน่วยงานผู้จัดพิมพ์", placeholder="เช่น กรมสนับสนุนบริการสุขภาพ")

    st.subheader("3. ระดับคุณภาพ")
    options = [k for k in jobs.PRESETS]
    preset = st.radio(
        "ระดับคุณภาพ", options, index=1, label_visibility="collapsed",
        format_func=lambda k: f"{jobs.PRESETS[k]['label']} — {jobs.PRESETS[k]['desc']}"
                              + ("" if jobs.has_key(jobs.PRESETS[k]["provider"])
                                 else "  (ยังไม่ได้ตั้งค่าคีย์)"))
    p = jobs.PRESETS[preset]

    st.subheader("4. เลขหน้า")
    st.caption("เลขที่พิมพ์ในเอกสารมักไม่ตรงกับลำดับหน้าในไฟล์ (เพราะมีปก คำนำ สารบัญ) "
               "ระบบหาความต่างนี้ให้อัตโนมัติ เพื่อให้อ้างอิง \"หน้า 96\" ได้ถูกต้อง")
    no_numbers = st.checkbox("เอกสารนี้ไม่มีเลขหน้าพิมพ์")
    key = f"offset::{up.name}::{n}"
    offset = None
    if not no_numbers:
        if st.button("ตรวจหาเลขหน้าอัตโนมัติ", disabled=not jobs.has_key(p["provider"])):
            with st.spinner("กำลังอ่านเลขหน้าจากตัวอย่าง 5 หน้า..."):
                st.session_state[key] = offset_detect.detect_offset(
                    doc, p["provider"], p["model"])
        det = st.session_state.get(key)
        if det:
            if det["offset"] is None:
                st.warning("อ่านเลขหน้าไม่ได้ กรุณากรอกเอง: ดูหน้าใดก็ได้ในโปรแกรมอ่าน PDF "
                           "แล้วคำนวณ (ลำดับหน้าในโปรแกรม − 1) − เลขที่พิมพ์")
            else:
                good = "✅" if det["confident"] else "⚠️ ไม่แน่ใจ ตรวจสอบอีกครั้ง —"
                st.write(f"{good} พบ {det['agree']}/{len(det['readings'])} หน้าที่สอดคล้องกัน")
        default = (det or {}).get("offset") or 0
        offset = st.number_input(
            "ค่าชดเชยเลขหน้า (page offset)", value=int(default), step=1,
            help="= ลำดับหน้าในไฟล์ (เริ่มนับ 0) − เลขที่พิมพ์บนหน้า")
        with st.expander("ตรวจสอบด้วยตาเอง"):
            printed = st.number_input("ดูหน้าที่พิมพ์เลข", min_value=1,
                                      value=max(1, min(n - offset, n // 2) if n > offset else 1))
            idx = printed + offset
            if 0 <= idx < n:
                st.image(doc[idx].get_pixmap(dpi=70).tobytes("png"),
                         caption=f"ลำดับในไฟล์ {idx + 1} — ควรเห็นเลข {printed} ที่ท้ายหรือหัวกระดาษ",
                         width=360)
            else:
                st.caption("หน้านี้อยู่นอกไฟล์")

    st.subheader("5. เลือกหน้า")
    scope = st.radio("หน้า", ["ทั้งเล่ม", "เฉพาะบางหน้า"], horizontal=True,
                     label_visibility="collapsed")
    try:
        if scope == "ทั้งเล่ม":
            pages = list(range(n))
        elif no_numbers:
            spec = st.text_input("ลำดับหน้าในไฟล์ (เริ่มที่ 1) เช่น 5, 10-20")
            pages = [p0 - 1 for p0 in common.parse_page_list(spec)] if spec else []
            pages = common._check_range(pages, n)
        else:
            spec = st.text_input("เลขหน้าที่พิมพ์ในเอกสาร เช่น 96 หรือ 96, 120-150")
            pages = common.resolve_pages("", spec, offset, n) if spec else []
    except (SystemExit, ValueError) as e:
        st.error(f"เลขหน้าไม่ถูกต้อง: {e}")
        pages = []
    if pages:
        st.caption(f"จะประมวลผล {len(pages)} หน้า")

    st.subheader("6. วิธีประมวลผล")
    modes = [MODE_NOW] + ([MODE_BATCH] if p["batch_ok"] else [])
    mode = st.radio("วิธี", modes, horizontal=True, label_visibility="collapsed",
                    format_func=lambda m: "เริ่มทันที (ดูความคืบหน้าได้)" if m == MODE_NOW
                    else "แบบประหยัด ลด 50% (ได้ผลภายใน 24 ชม.)")

    est = jobs.estimate_thb(doc, pages, settings["dpi"], preset, mode == MODE_BATCH,
                            settings) if pages else None
    over = bool(est and est[1] > settings["budget_limit_thb"])
    if est:
        st.metric("ค่าใช้จ่ายโดยประมาณ", f"{est[0]:,.0f} – {est[1]:,.0f} บาท")
        if over:
            st.error(f"เกินวงเงินต่องาน ({settings['budget_limit_thb']:,.0f} บาท) "
                     "ลดจำนวนหน้า เลือกระดับที่ถูกกว่า หรือติดต่อผู้ดูแลระบบ")

    ready = bool(pages) and jobs.has_key(p["provider"]) and not over
    if st.button("เริ่มแปลงเอกสาร", type="primary", disabled=not ready):
        job = jobs.create_job(pdf_bytes, up.name, title=title, publisher=publisher,
                              preset=preset, mode=mode, pages=pages,
                              offset=None if no_numbers else int(offset),
                              dpi=settings["dpi"], estimate_thb=est)
        if mode == MODE_NOW:
            jobs.start(job["id"], settings["workers"])
        else:
            jobs.submit_batch(job["id"])
        st.session_state["open_job"] = job["id"]
        st.session_state["goto"] = "งานทั้งหมด"
        st.rerun()


# ---------------- job list / detail ----------------

def jobs_page():
    all_jobs = jobs.list_jobs()
    if not all_jobs:
        st.info("ยังไม่มีงาน เริ่มจากเมนู \"งานใหม่\"")
        return
    ids = [j["id"] for j in all_jobs]
    current = st.session_state.get("open_job")
    idx = ids.index(current) if current in ids else 0
    labels = {j["id"]: f"{j['title'] or j['filename']} — {status_th(j)} ({j['created'][:16]})"
              for j in all_jobs}
    job_id = st.selectbox("เลือกงาน", ids, index=idx, format_func=labels.get)
    st.session_state["open_job"] = job_id
    job_detail(job_id)


@st.fragment(run_every="4s")
def live_progress(job_id):
    job = jobs.load_job(job_id)
    c = jobs.progress(job)
    status = jobs.effective_status(job)
    st.write(f"**สถานะ:** {STATUS_TH.get(status, status)}")
    total = max(1, c["total"])
    st.progress((c["done"] + c["failed"]) / total,
                text=f"สำเร็จ {c['done']} · ไม่สำเร็จ {c['failed']} · เหลือ {c['pending']} "
                     f"จาก {c['total']} หน้า")
    if job.get("error"):
        st.error(job["error"])
    if status not in ("running", "submitting"):
        st.rerun()        # refresh the whole page once, to show results


def job_detail(job_id):
    job = jobs.load_job(job_id)
    p = jobs.PRESETS.get(job["preset"], {})
    status = jobs.effective_status(job)
    st.subheader(job["title"] or job["filename"])
    st.caption(f"{job['filename']} · {p.get('label', job['model'])} · "
               f"{'เริ่มทันที' if job['mode'] == MODE_NOW else 'แบบประหยัด (batch)'} · "
               f"{len(job['pages'])} หน้า"
               + (f" · ประมาณ {job['estimate_thb'][0]:,.0f}–{job['estimate_thb'][1]:,.0f} บาท"
                  if job.get("estimate_thb") else ""))

    if status in ("running", "submitting"):
        live_progress(job_id)
        if status == "running" and st.button("หยุดชั่วคราว"):
            jobs.stop(job_id)
            st.toast("กำลังหยุดหลังหน้าที่กำลังทำอยู่เสร็จ")
        return

    c = jobs.progress(job)
    st.write(f"**สถานะ:** {STATUS_TH.get(status, status)}")
    st.progress((c["done"] + c["failed"]) / max(1, c["total"]),
                text=f"สำเร็จ {c['done']} · ไม่สำเร็จ {c['failed']} · เหลือ {c['pending']} "
                     f"จาก {c['total']} หน้า")
    if job.get("error"):
        st.error(job["error"])

    b1, b2, _ = st.columns([1, 1, 3])
    if status == "submitted":
        if b1.button("ตรวจสอบผล", type="primary"):
            try:
                st.toast(jobs.check_batch(job_id))
            except Exception as e:
                st.error(f"ตรวจสอบไม่สำเร็จ: {e}")
            st.rerun()
    elif job["mode"] == MODE_NOW and (c["pending"] or c["failed"]):
        if b1.button("ทำต่อ / ลองหน้าที่ไม่สำเร็จอีกครั้ง", type="primary"):
            jobs.start(job_id, settings["workers"])
            st.rerun()
    elif status == "failed" and job["mode"] == MODE_BATCH:
        if b1.button("ส่งงานอีกครั้ง", type="primary"):
            jobs.submit_batch(job_id)
            st.rerun()

    z = jobs.output_zip(job)
    if z:
        b2.download_button("ดาวน์โหลดผลลัพธ์ (.zip)", z,
                           file_name=f"{job['id']}.zip", mime="application/zip")

    if c["done"] or c["failed"]:
        review(job)


def review(job):
    st.divider()
    st.subheader("ตรวจทาน")
    flags = jobs.flagged_pages(job)
    offset = job["page_offset"]
    label = lambda pno: (f"หน้า {pno - offset}" if offset is not None and pno - offset >= 1
                         else f"ลำดับ {pno + 1}")
    if flags:
        st.warning(f"มี {len(flags)} หน้าที่ควรตรวจ (ระบบตรวจพบอัตโนมัติ)")
    else:
        st.success("ไม่พบหน้าที่น่าสงสัย ลองสุ่มตรวจสัก 2–3 หน้า")

    only_flagged = st.toggle("แสดงเฉพาะหน้าที่ควรตรวจ", value=bool(flags))
    choices = sorted(flags) if only_flagged and flags else sorted(job["pages"])
    pno = st.selectbox(
        "เลือกหน้า", choices,
        format_func=lambda x: label(x) + (" ⚠️ " + " / ".join(flags[x]) if x in flags else ""))

    rows = jobs.page_rows(job)
    row = rows.get(pno, {})
    left, right = st.columns(2)
    left.image(jobs.page_png(job, pno), caption=f"{label(pno)} (ลำดับในไฟล์ {pno + 1})",
               width="stretch")
    with right:
        if pno in flags:
            for f in flags[pno]:
                st.warning(f)
        text = st.text_area("ข้อความที่ถอดได้ (แก้ไขได้)", row.get("markdown") or "",
                            height=520, key=f"edit-{job['id']}-{pno}")
        c1, c2 = st.columns(2)
        if c1.button("บันทึกการแก้ไข", key=f"save-{pno}"):
            jobs.save_edit(job["id"], pno, text)
            st.toast("บันทึกแล้ว และอัปเดตไฟล์ผลลัพธ์")
            st.rerun()
        stronger = [k for k in jobs.PRESETS if k != job["preset"]
                    and jobs.has_key(jobs.PRESETS[k]["provider"])]
        if stronger:
            k = c2.selectbox("อ่านหน้านี้ใหม่ด้วย", stronger,
                             format_func=lambda k: jobs.PRESETS[k]["label"],
                             key=f"redo-model-{pno}")
            if c2.button("อ่านใหม่", key=f"redo-{pno}"):
                with st.spinner("กำลังอ่านหน้านี้ใหม่..."):
                    ok = jobs.redo_page(job["id"], pno, k)
                (st.toast if ok else st.error)(
                    "อ่านใหม่เรียบร้อย" if ok else "อ่านใหม่ไม่สำเร็จ ข้อความเดิมยังอยู่")
                st.rerun()
        if pno in job.get("edited_pages", []):
            st.caption("หน้านี้แก้ไขด้วยมือแล้ว")


# ---------------- settings ----------------

def settings_page():
    st.header("ตั้งค่า (สำหรับผู้ดูแลระบบ)")
    s = dict(settings)
    s["thb_per_usd"] = st.number_input("อัตราแลกเปลี่ยน (บาท ต่อ 1 USD)",
                                       value=float(s["thb_per_usd"]), step=0.5,
                                       help="ใช้คำนวณค่าใช้จ่ายโดยประมาณ ควรปรับตามอัตราปัจจุบัน")
    s["budget_limit_thb"] = st.number_input("วงเงินสูงสุดต่องาน (บาท)",
                                            value=float(s["budget_limit_thb"]), step=100.0)
    s["workers"] = st.slider("จำนวนหน้าที่ประมวลผลพร้อมกัน", 1, 16, int(s["workers"]),
                             help="มากขึ้น = เร็วขึ้น แต่อาจติดขีดจำกัดของผู้ให้บริการ")
    s["dpi"] = st.select_slider("ความละเอียดภาพ (DPI)", [120, 150, 200], value=int(s["dpi"]),
                                help="200 ช่วยให้อ่านตารางแน่นๆ ได้ดีขึ้นกับ Claude แต่แพงขึ้น")
    if st.button("บันทึกการตั้งค่า", type="primary"):
        jobs.save_settings(s)
        st.success("บันทึกแล้ว")
    st.divider()
    st.caption("คีย์ API ตั้งค่าในไฟล์ .env บนเครื่องที่รันโปรแกรมนี้ ใช้ `python check_keys.py --ping` "
               "เพื่อตรวจสอบ ระดับคุณภาพ/โมเดลแก้ได้ที่ PRESETS ใน jobs.py")


if page == "งานใหม่":
    new_job_page()
elif page == "งานทั้งหมด":
    jobs_page()
else:
    settings_page()

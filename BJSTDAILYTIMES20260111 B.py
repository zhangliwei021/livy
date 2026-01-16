# coding=utf-8
"""
巴基斯坦《每日时报》(Daily Times) 电子报下载器
支持日期：YYYYMMDD → 转换为 DD-MM-YYYY
依赖：requests、Pillow、PyPDF2（合并用）
"""
import os, sys, time, re, shutil, ctypes, queue, threading, traceback, warnings
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

# 忽略 Pillow / PyPDF2 警告
warnings.filterwarnings("ignore", category=UserWarning)

try:
    from PIL import Image
except ImportError:
    messagebox.showerror("缺少依赖", "请先安装：pip install Pillow")
    sys.exit(1)

# ==================== 配置 ====================
BASE_URL = "https://dailytimes.com.pk"
DOWNLOAD_ROOT = r"D:\INFO\dailytimes_dl"
FINAL_DIR = r"D:\INFO\dailytimes"
MAX_PAGES = 48  # 最大尝试页数 a1.jpg ～ a48.jpg
# ==================================================

class ThreadSafeLogger:
    def __init__(self, log_queue):
        self.log_queue = log_queue
    def log(self, msg, level="INFO"):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log_queue.put(("LOG", f"[{ts}] [{level}] {msg}"))

def download_image(session, img_url, save_path, logger):
    """下载单页 JPG"""
    try:
        resp = session.get(img_url, stream=True, timeout=30)
        if resp.status_code == 404:
            return False  # 404 表示该页不存在
        resp.raise_for_status()
        with open(save_path, 'wb') as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        logger.log(f"✅ 已下载: {os.path.basename(save_path)}")
        return True
    except Exception as e:
        logger.log(f"❌ 下载失败 {img_url}: {e}")
        if os.path.exists(save_path):
            os.remove(save_path)
        return False

def images_to_pdf(jpg_files, output_pdf, logger):
    """将 JPG 合并为 PDF"""
    try:
        imgs = []
        for jpg in jpg_files:
            im = Image.open(jpg)
            if im.mode != "RGB":
                im = im.convert("RGB")
            imgs.append(im)
        if not imgs:
            logger.log("⚠️ 没有可合并的图片")
            return None
        imgs[0].save(output_pdf, save_all=True, append_images=imgs[1:])
        logger.log(f"📄 合并完成: {os.path.basename(output_pdf)}")
        return output_pdf
    except Exception as e:
        logger.log(f"❌ 合并 PDF 失败: {e}")
        return None

def run_download_async(date_str, log_queue, status_queue, root):
    """后台线程任务：下载 Daily Times 电子报"""
    logger = ThreadSafeLogger(log_queue)
    try:
        # 清理缓存目录
        if os.path.exists(DOWNLOAD_ROOT):
            shutil.rmtree(DOWNLOAD_ROOT, ignore_errors=True)
        os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

        target_dir = os.path.join(DOWNLOAD_ROOT, date_str)
        os.makedirs(target_dir, exist_ok=True)

        # 转换日期格式 YYYYMMDD → DD-MM-YYYY
        try:
            dt = datetime.strptime(date_str, "%Y%m%d")
            epaper_date = dt.strftime("%d-%m-%Y")
        except ValueError:
            raise ValueError("日期格式错误")

        status_queue.put(("STATUS", f"开始下载 {date_str} ({epaper_date})…"))
        status_queue.put(("PROGRESS", 10))

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

        # 第1步：访问日期页面
        index_url = f"{BASE_URL}/epaper/{epaper_date}/"
        logger.log(f"正在访问索引页: {index_url}")
        resp = session.get(index_url, timeout=20)
        if resp.status_code == 404:
            raise Exception(f"该日期无电子报: {epaper_date}")
        resp.raise_for_status()

        # 第2步：解析 p 值
        soup = BeautifulSoup(resp.text, "html.parser")
        shortlink_tag = soup.find("link", {"rel": "shortlink", "href": True})
        if not shortlink_tag:
            raise Exception("未找到 shortlink，无法提取文章ID")

        href = shortlink_tag["href"]
        match = re.search(r'[?&]p=(\d+)', href)
        if not match:
            raise Exception("无法从 shortlink 提取 p 值")
        p_value = match.group(1)
        logger.log(f"✅ 提取到文章ID: p={p_value}")

        # 第3步：尝试下载 a1.jpg 到 a48.jpg
        jpg_files = []
        valid_pages = 0
        for n in range(1, MAX_PAGES + 1):
            progress = 10 + int((n / MAX_PAGES) * 80)
            status_queue.put(("PROGRESS", progress))
            status_queue.put(("STATUS", f"正在下载第 {n} 页…"))

            img_url = f"{BASE_URL}/assets/uploads/epaper/{p_value}/a{n}.jpg"
            jpg_path = os.path.join(target_dir, f"page_{n:02d}.jpg")

            if download_image(session, img_url, jpg_path, logger):
                jpg_files.append(jpg_path)
                valid_pages += 1
            else:
                logger.log(f"⏹️ 第 {n} 页不存在或下载失败，停止后续尝试")
                break  # 一旦失败，后续页大概率也不存在

        if not jpg_files:
            status_queue.put(("STATUS", "❌ 未下载到任何页面"))
            status_queue.put(("MESSAGE", ("error", "错误", "所有页面下载失败或该日期无内容")))
            return

        logger.log(f"共下载 {len(jpg_files)} 页")

        status_queue.put(("PROGRESS", 90))
        status_queue.put(("STATUS", "正在生成 PDF…"))

        # 合并 PDF
        pdf_name = f"DailyTimes_{date_str}.pdf"
        pdf_path = os.path.join(target_dir, pdf_name)
        final_pdf = images_to_pdf(jpg_files, pdf_path, logger)

        if not final_pdf:
            status_queue.put(("STATUS", "❌ 合并 PDF 失败"))
            status_queue.put(("MESSAGE", ("error", "错误", "合并 PDF 失败")))
            return

        # 保存到最终目录
        os.makedirs(FINAL_DIR, exist_ok=True)
        target_pdf = os.path.join(FINAL_DIR, os.path.basename(final_pdf))
        shutil.copy2(final_pdf, target_pdf)

        # 清理缓存
        shutil.rmtree(target_dir, ignore_errors=True)

        # 自动打开文件夹（Windows）
        if sys.platform.startswith("win"):
            try:
                os.startfile(FINAL_DIR)
                logger.log("📁 已自动打开目标文件夹")
            except Exception as e:
                logger.log(f"⚠️ 无法打开目标文件夹: {e}")

        status_queue.put(("PROGRESS", 100))
        status_queue.put(("STATUS", f"✅ 下载完成！共 {len(jpg_files)} 页"))
        status_queue.put(("MESSAGE", ("info", "完成",
                                      f"Daily Times {date_str} 下载完成！\n保存至：{target_pdf}")))

    except Exception as e:
        error_msg = str(e)
        logger.log(f"❌ 任务失败: {error_msg}")
        traceback.print_exc()
        status_queue.put(("STATUS", f"❌ 下载失败：{error_msg[:100]}"))
        status_queue.put(("MESSAGE", ("error", "错误", f"下载失败：{error_msg[:100]}")))
    finally:
        status_queue.put(("DONE", None))

# ==================== GUI 部分 ====================
class DailyTimesDownloader:
    def __init__(self, root):
        self.root = root
        self.root.title("巴基斯坦《每日时报》电子报下载器")
        self.root.geometry("800x600")
        self.root.configure(bg='#f8f9fa')
        self.center_window()
        self.create_ui()
        self.log_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.check_queues()

    def center_window(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (w // 2)
        y = (self.root.winfo_screenheight() // 2) - (h // 2)
        self.root.geometry(f'{w}x{h}+{x}+{y}')

    def create_ui(self):
        tk.Label(self.root, text="巴基斯坦《每日时报》电子报下载器",
                 font=("Microsoft YaHei", 24, "bold"),
                 bg='#f8f9fa', fg='#495057').pack(pady=20)
        tk.Label(self.root,
                 text="本程序用于下载 Daily Times 电子报并合并为单个 PDF",
                 font=("Microsoft YaHei", 12),
                 bg='#f8f9fa', fg='#6c757d').pack(pady=10)

        date_frame = tk.Frame(self.root, bg='#f8f9fa')
        date_frame.pack(pady=10)
        tk.Label(date_frame, text="请输入日期（YYYYMMDD）:",
                 font=("Microsoft YaHei", 11),
                 bg='#f8f9fa', fg='#495057').pack(side=tk.LEFT, padx=(0, 10))
        self.date_entry = tk.Entry(date_frame, font=("Microsoft YaHei", 11), width=15)
        self.date_entry.pack(side=tk.LEFT)
        self.date_entry.insert(0, datetime.now().strftime("%Y%m%d"))

        btn_frame = tk.Frame(self.root, bg='#f8f9fa')
        btn_frame.pack(pady=20)
        self.download_btn = tk.Button(btn_frame, text="开始下载",
                                      font=("Microsoft YaHei", 14),
                                      bg='#0d6efd', fg='white',
                                      activebackground='#0b5ed7',
                                      relief='flat', padx=30, pady=10,
                                      command=self.start_download)
        self.download_btn.pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="查看/选择日期",
                  font=("Microsoft YaHei", 14),
                  bg='#28a745', fg='white',
                  activebackground='#218838',
                  relief='flat', padx=30, pady=10,
                  command=self.view_or_select_date).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="退出程序",
                  font=("Microsoft YaHei", 14),
                  bg='#6c757d', fg='white',
                  activebackground='#5a6268',
                  relief='flat', padx=30, pady=10,
                  command=self.root.destroy).pack(side=tk.LEFT, padx=10)

        self.status_label = tk.Label(self.root, text="就绪",
                                     font=("Microsoft YaHei", 12),
                                     bg='#f8f9fa', fg='#28a745')
        self.status_label.pack(pady=10)
        self.progress_bar = ttk.Progressbar(self.root, length=500, mode='determinate')
        self.progress_bar.pack(pady=10)

        log_frame = tk.LabelFrame(self.root, text="操作日志",
                                  font=("Microsoft YaHei", 11, "bold"),
                                  bg='white', fg='#495057', padx=10, pady=10)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        self.log_text = tk.Text(log_frame, height=10, font=("Courier", 9), wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        scroll = tk.Scrollbar(self.log_text)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=scroll.set)
        scroll.config(command=self.log_text.yview)

    def view_or_select_date(self):
        today = datetime.now().strftime("%Y%m%d")
        cur = self.date_entry.get().strip() or today
        new = simpledialog.askstring("选择日期",
                                     f"当前日期: {cur}\n请输入新日期（YYYYMMDD）：\n留空则使用今天（{today}）：",
                                     initialvalue=cur, parent=self.root)
        if new is not None:
            new = new.strip() or today
            if re.match(r"^\d{8}$", new):
                try:
                    datetime.strptime(new, "%Y%m%d")
                    self.date_entry.delete(0, tk.END)
                    self.date_entry.insert(0, new)
                except ValueError:
                    messagebox.showerror("错误", "日期不合法", parent=self.root)
            else:
                messagebox.showerror("错误", "日期格式应为 YYYYMMDD", parent=self.root)

    def validate_date(self, date_str):
        if not re.match(r"^\d{8}$", date_str):
            messagebox.showerror("错误", "日期格式无效，请使用 YYYYMMDD 格式", parent=self.root)
            return False
        try:
            datetime.strptime(date_str, "%Y%m%d")
            return True
        except ValueError:
            messagebox.showerror("错误", "日期不合法", parent=self.root)
            return False

    def start_download(self):
        date_str = self.date_entry.get().strip()
        if not date_str:
            date_str = datetime.now().strftime("%Y%m%d")
            self.date_entry.delete(0, tk.END)
            self.date_entry.insert(0, date_str)
        if not self.validate_date(date_str):
            return
        self.log_text.delete(1.0, tk.END)
        self.download_btn.config(state=tk.DISABLED)
        self.progress_bar['value'] = 0
        threading.Thread(target=run_download_async,
                         args=(date_str, self.log_queue, self.status_queue, self.root),
                         daemon=True).start()

    def check_queues(self):
        while not self.log_queue.empty():
            try:
                msg_type, content = self.log_queue.get_nowait()
                if msg_type == "LOG":
                    self.log_text.insert(tk.END, content + "\n")
                    self.log_text.see(tk.END)
            except queue.Empty:
                break
        while not self.status_queue.empty():
            try:
                msg_type, data = self.status_queue.get_nowait()
                if msg_type == "STATUS":
                    self.status_label.config(text=data, fg=self._get_status_color(data))
                elif msg_type == "PROGRESS":
                    self.progress_bar['value'] = data
                elif msg_type == "MESSAGE":
                    mtype, title, msg = data
                    getattr(messagebox, f"show{mtype}")(title, msg, parent=self.root)
                elif msg_type == "DONE":
                    self.download_btn.config(state=tk.NORMAL)
            except queue.Empty:
                break
        self.root.after(100, self.check_queues)

    def _get_status_color(self, text):
        if text.startswith("✅"):
            return "#28a745"
        elif text.startswith("❌"):
            return "#dc3545"
        elif text.startswith("⚠️") or text.startswith("⏹️"):
            return "#ffc107"
        else:
            return "#0d6efd"

if __name__ == '__main__':
    if sys.platform.startswith('win'):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except:
            pass
    root = tk.Tk()
    DailyTimesDownloader(root)
    root.mainloop()

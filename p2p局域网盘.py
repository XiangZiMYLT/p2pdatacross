import os
import sys
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import requests
from flask import Flask, send_from_directory, jsonify, request
import socket
import json
from datetime import datetime
import queue  # 线程安全队列（子线程→主线程GUI通信）

# --- 全局配置 (无需用户修改) ---
NODE_NAME = socket.gethostname()
FILE_SERVER_PORT = 5000
SHARED_FOLDER = 'my_shared_files'
DOWNLOAD_FOLDER = 'downloads'
DIRECTORY_SERVER_PORT = 5001
NODE_TIMEOUT = 15
# 选举相关配置
ELECTION_BROADCAST_PORT = 5002
ELECTION_TIMEOUT = 3  # 选举超时时间(秒)
HEARTBEAT_INTERVAL = 5  # 心跳间隔(秒)

# --- 1. 自动安装依赖库 ---
def install_dependencies():
    required_packages = ['flask', 'requests']
    for package in required_packages:
        try:
            __import__(package)
        except ImportError:
            print(f"'{package}' 未安装，正在尝试安装...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package])
            except Exception as e:
                messagebox.showerror("依赖安装失败", f"无法安装 '{package}'。请手动运行 'pip install {package}' 后重试。")
                sys.exit(1)

install_dependencies()

# --- 2. 工具函数 ---
def get_local_ip():
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        if s:
            s.close()
    return ip

LOCAL_IP = get_local_ip()

# --- 3. Flask 服务 (每个节点都运行) ---
app = Flask(__name__)
if not os.path.exists(SHARED_FOLDER): os.makedirs(SHARED_FOLDER)
if not os.path.exists(DOWNLOAD_FOLDER): os.makedirs(DOWNLOAD_FOLDER)

@app.route('/api/files')
def get_file_list():
    try:
        files = [f for f in os.listdir(SHARED_FOLDER) if os.path.isfile(os.path.join(SHARED_FOLDER, f))]
        return jsonify({"files": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    try:
        return send_from_directory(SHARED_FOLDER, filename, as_attachment=True)
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if file:
        filepath = os.path.join(SHARED_FOLDER, file.filename)
        file.save(filepath)
        return jsonify({"message": "File uploaded successfully"}), 200

def run_file_server():
    app.run(host='0.0.0.0', port=FILE_SERVER_PORT, debug=False, use_reloader=False)

# --- 4. 目录服务器 (自动选举后启动) ---
directory_app = Flask(__name__)
online_nodes = {}  # { "node_id": { "name": "NodeA", "ip": "x.x.x.x", "port": 5000, "last_seen": timestamp } }

@directory_app.route('/api/register', methods=['POST'])
def register_node():
    data = request.json
    node_id = f"{data['ip']}:{data['port']}"
    online_nodes[node_id] = {
        "name": data.get("name", node_id),
        "ip": data["ip"],
        "port": data["port"],
        "last_seen": datetime.timestamp(datetime.now())
    }
    return jsonify({"status": "ok"})

@directory_app.route('/api/nodes')
def get_nodes():
    now = datetime.timestamp(datetime.now())
    to_remove = [n for n, info in online_nodes.items() if (now - info["last_seen"]) > NODE_TIMEOUT]
    for n in to_remove: del online_nodes[n]
    return jsonify({
        "nodes": [{"id": k, "name": v["name"], "ip": v["ip"], "port": v["port"]} for k, v in online_nodes.items()]
    })

def run_directory_server():
    if is_directory_server:
        directory_app.run(host='0.0.0.0', port=DIRECTORY_SERVER_PORT, debug=False, use_reloader=False)

# --- 5. 自动化选举模块 ---
is_directory_server = False
directory_server_ip = None
election_lock = threading.Lock()

def elect_directory_server():
    global is_directory_server, directory_server_ip
    with election_lock:
        # 步骤1: 发送UDP广播，寻找现有目录服务器
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.0)
        found_server = None

        try:
            # 发送选举请求
            request_msg = json.dumps({"type": "election_request", "node_ip": LOCAL_IP}).encode('utf-8')
            for _ in range(3):  # 发送3次确保被收到
                sock.sendto(request_msg, ('<broadcast>', ELECTION_BROADCAST_PORT))
                time.sleep(0.5)

            # 等待响应 (最长ELECTION_TIMEOUT秒)
            start_time = time.time()
            while time.time() - start_time < ELECTION_TIMEOUT:
                try:
                    data, addr = sock.recvfrom(1024)
                    resp = json.loads(data.decode('utf-8'))
                    if resp.get("type") == "directory_server_response":
                        # 发现现有目录服务器，记录其IP
                        found_server = resp["server_ip"]
                        break
                except socket.timeout:
                    continue
        finally:
            sock.close()

        # 步骤2: 处理选举结果
        if found_server:
            # 找到现有目录服务器，自己作为普通节点
            is_directory_server = False
            directory_server_ip = found_server
            print(f"✅ 发现目录服务器: {directory_server_ip}，本机作为普通节点")
            return

        # 步骤3: 未找到现有服务器，自己竞选目录服务器
        # 再次广播，确认没有其他节点同时竞选
        time.sleep(1)  # 等待其他节点的广播
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.0)
        potential_servers = [LOCAL_IP]

        try:
            # 发送竞选声明
            announce_msg = json.dumps({"type": "election_announce", "node_ip": LOCAL_IP}).encode('utf-8')
            sock.sendto(announce_msg, ('<broadcast>', ELECTION_BROADCAST_PORT))

            # 收集其他竞选者
            start_time = time.time()
            while time.time() - start_time < 1:
                try:
                    data, addr = sock.recvfrom(1024)
                    resp = json.loads(data.decode('utf-8'))
                    if resp.get("type") == "election_announce" and resp["node_ip"] != LOCAL_IP:
                        potential_servers.append(resp["node_ip"])
                except socket.timeout:
                    continue
        finally:
            sock.close()

        # 步骤4: 从竞选者中选出目录服务器（IP最小的获胜）
        potential_servers.sort()
        elected_server = potential_servers[0]
        if elected_server == LOCAL_IP:
            # 自己当选目录服务器
            is_directory_server = True
            directory_server_ip = LOCAL_IP
            print(f"🎉🎉 本机当选目录服务器 (IP: {LOCAL_IP})")
        else:
            # 其他节点当选，自己作为普通节点
            is_directory_server = False
            directory_server_ip = elected_server
            print(f"✅ 选举完成，目录服务器: {directory_server_ip}，本机作为普通节点")

# --- 6. 选举响应监听 (所有节点都运行) ---
def listen_for_election():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(('', ELECTION_BROADCAST_PORT))
        sock.settimeout(1.0)
        while True:
            try:
                data, addr = sock.recvfrom(1024)
                msg = json.loads(data.decode('utf-8'))
                
                if msg.get("type") == "election_request":
                    # 收到选举请求，若自己是目录服务器则响应
                    if is_directory_server:
                        resp_msg = json.dumps({
                            "type": "directory_server_response",
                            "server_ip": LOCAL_IP
                        }).encode('utf-8')
                        sock.sendto(resp_msg, addr)
                
                elif msg.get("type") == "election_announce" and msg["node_ip"] != LOCAL_IP:
                    # 收到其他节点的竞选声明，无需处理（竞选逻辑在elect_directory_server中）
                    pass
            except socket.timeout:
                continue
            except Exception as e:
                print(f"选举监听错误: {e}")
                break
    finally:
        sock.close()

# --- 7. 节点管理器 (自动连接目录服务器，线程安全GUI更新) ---
class NodeManager:
    def __init__(self, gui_task_queue):
        # 接收主线程的GUI任务队列（子线程仅提交任务，不执行GUI操作）
        self.gui_task_queue = gui_task_queue
        self.is_running = True
        self.online_nodes = []  # 子线程本地缓存的节点列表
        self.update_thread = threading.Thread(target=self.periodic_update, daemon=True)
        self.update_thread.start()

    def periodic_update(self):
        global directory_server_ip
        while self.is_running:
            try:
                # 1. 检查目录服务器可用性，不可用则重新选举
                if not self.check_directory_server():
                    error_msg = "目录服务器连接失败，重新选举..."
                    print(f"⚠️  {error_msg}")
                    self._submit_gui_task("log", message=error_msg)
                    
                    elect_directory_server()
                    self._submit_gui_task("log", message=f"目录服务器切换至: {directory_server_ip}")

                # 2. 向目录服务器发送心跳/注册
                try:
                    requests.post(
                        f"http://{directory_server_ip}:{DIRECTORY_SERVER_PORT}/api/register",
                        json={"name": NODE_NAME, "ip": LOCAL_IP, "port": FILE_SERVER_PORT},
                        timeout=3
                    )
                except Exception as e:
                    error_msg = f"心跳失败: {str(e)}"
                    print(f"⚠️  {error_msg}")
                    self._submit_gui_task("log", message=f"与目录服务器通信失败: {directory_server_ip}")
                    time.sleep(2)
                    continue

                # 3. 获取在线节点列表并更新GUI
                try:
                    resp = requests.get(
                        f"http://{directory_server_ip}:{DIRECTORY_SERVER_PORT}/api/nodes",
                        timeout=3
                    )
                    resp.raise_for_status()
                    self.online_nodes = resp.json().get("nodes", [])
                    # 提交节点列表更新任务（主线程执行）
                    self._submit_gui_task("update_nodes", nodes=self.online_nodes)
                    self._submit_gui_task("log", message=f"获取节点列表成功 ({len(self.online_nodes)} 个节点)")
                except Exception as e:
                    error_msg = f"获取节点列表失败: {str(e)}"
                    print(f"⚠️  {error_msg}")
                    self._submit_gui_task("log", message="无法获取节点列表")

                time.sleep(HEARTBEAT_INTERVAL)

            except Exception as e:
                # 捕获子线程所有异常，避免线程崩溃
                error_msg = f"节点管理器线程异常: {str(e)}"
                print(f"❌❌  {error_msg}")
                self._submit_gui_task("error", error_msg=error_msg)
                time.sleep(5)  # 异常后延迟重试

    def _submit_gui_task(self, task_type, **kwargs):
        """
        子线程安全提交GUI任务到队列（非阻塞，避免队列满时卡住子线程）
        :param task_type: 任务类型（log/update_nodes/error）
        :param kwargs: 任务参数（message/nodes/error_msg）
        """
        try:
            self.gui_task_queue.put_nowait({"type": task_type, **kwargs})
        except queue.Full:
            print(f"⚠️  GUI任务队列已满，丢弃任务: {task_type}")

    def check_directory_server(self):
        """检查目录服务器是否可达"""
        if not directory_server_ip:
            return False
        try:
            resp = requests.get(f"http://{directory_server_ip}:{DIRECTORY_SERVER_PORT}/api/nodes", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    def stop(self):
        self.is_running = False

# --- 8. Tkinter GUI (线程安全版本) ---
class AutoP2PFileShareApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"全自动P2P文件共享 - {NODE_NAME} (文件端口: {FILE_SERVER_PORT})")
        self.geometry("700x600")
        global app_instance
        app_instance = self

        # 1. 创建线程安全GUI任务队列（子线程→主线程通信）
        self.gui_task_queue = queue.Queue(maxsize=100)

        # 2. 启动核心服务（先初始化队列，再启动子线程）
        self.start_core_services()

        # 3. 初始化UI
        self.create_widgets()
        self._safe_log(f"✅ 文件服务启动成功，共享文件夹: {os.path.abspath(SHARED_FOLDER)}")
        self._safe_log(f"📡📡 目录服务器: {directory_server_ip} (本机{(is_directory_server and '是' or '不是')}目录服务器)")

        # 4. 启动主线程队列轮询（持续处理子线程GUI任务）
        self.process_gui_queue()

    def start_core_services(self):
        """启动所有核心服务线程"""
        # 1. 选举响应监听线程
        self.election_listen_thread = threading.Thread(target=listen_for_election, daemon=True)
        self.election_listen_thread.start()

        # 2. 执行目录服务器选举
        elect_directory_server()

        # 3. 启动目录服务器（若当选）
        self.directory_server_thread = threading.Thread(target=run_directory_server, daemon=True)
        self.directory_server_thread.start()

        # 4. 启动文件服务器
        self.file_server_thread = threading.Thread(target=run_file_server, daemon=True)
        self.file_server_thread.start()

        # 5. 启动节点管理器（传递GUI队列，确保线程安全）
        self.node_manager = NodeManager(gui_task_queue=self.gui_task_queue)

    def create_widgets(self):
        # 节点列表
        node_frame = ttk.LabelFrame(self, text="在线节点 (自动发现)")
        node_frame.pack(padx=10, pady=5, fill="x")
        
        # 关键修复：添加 exportselection=False 防止焦点丢失时取消选择
        self.node_listbox = tk.Listbox(node_frame, selectmode=tk.SINGLE, height=4, exportselection=False)
        self.node_listbox.pack(side="left", fill="both", expand=True, padx=10, pady=5)
        
        # 修改绑定：使用鼠标点击事件而不是选择变化事件
        self.node_listbox.bind("<Button-1>", self.on_node_click)
        node_scroll = ttk.Scrollbar(node_frame, orient="vertical", command=self.node_listbox.yview)
        node_scroll.pack(side="right", fill="y", pady=5)
        self.node_listbox.config(yscrollcommand=node_scroll.set)

        # 文件列表
        file_frame = ttk.LabelFrame(self, text="文件列表 (点击节点查看)")
        file_frame.pack(padx=10, pady=5, fill="both", expand=True)
        
        # 文件列表也添加 exportselection=False
        self.file_listbox = tk.Listbox(file_frame, selectmode=tk.SINGLE, exportselection=False)
        self.file_listbox.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        self.file_listbox.bind("<<ListboxSelect>>", self.on_file_select)
        file_scroll = ttk.Scrollbar(file_frame, orient="vertical", command=self.file_listbox.yview)
        file_scroll.pack(side="right", fill="y", pady=10)
        self.file_listbox.config(yscrollcommand=file_scroll.set)

        # 按钮区
        btn_frame = ttk.Frame(self)
        btn_frame.pack(padx=10, pady=5, fill="x")
        self.refresh_btn = ttk.Button(btn_frame, text="刷新文件列表", command=self.refresh_file_list, state="disabled")
        self.refresh_btn.pack(side="left", padx=5)
        self.download_btn = ttk.Button(btn_frame, text="下载选中文件", command=self.download_selected_file, state="disabled")
        self.download_btn.pack(side="left", padx=5)
        self.upload_btn = ttk.Button(btn_frame, text="上传文件到该节点", command=self.upload_file_to_node, state="disabled")
        self.upload_btn.pack(side="left", padx=5)
        ttk.Label(btn_frame, text="").pack(side="left", expand=True)
        self.delete_btn = ttk.Button(btn_frame, text="删除本地文件", command=self.delete_local_file, state="disabled")
        self.delete_btn.pack(side="left", padx=5)

        # 日志区
        log_frame = ttk.LabelFrame(self, text="操作日志 (自动选举状态已显示)")
        log_frame.pack(padx=10, pady=5, fill="both", expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=6, state='disabled')
        self.log_text.pack(padx=10, pady=5, fill="both", expand=True)

    def process_gui_queue(self):
        """
        主线程轮询GUI任务队列（每隔100ms执行一次）
        所有GUI操作都通过这个方法执行，确保线程安全
        """
        try:
            # 批量处理队列中的所有任务（非阻塞）
            while True:
                task = self.gui_task_queue.get_nowait()
                task_type = task.get("type")

                if task_type == "log":
                    self._safe_log(task["message"])
                elif task_type == "update_nodes":
                    self._safe_update_node_listbox(task["nodes"])
                elif task_type == "error":
                    self._safe_log(f"❌❌ 线程错误: {task['error_msg']}")
        except queue.Empty:
            pass
        finally:
            # 继续轮询（关键：确保主线程持续处理任务）
            self.after(100, self.process_gui_queue)

    def _safe_log(self, message):
        """主线程安全日志记录（仅主线程调用）"""
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, f"{time.strftime('%H:%M:%S')} - {message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')

    def _safe_update_node_listbox(self, nodes):
        """主线程安全更新节点列表（仅主线程调用）"""
        self.node_listbox.delete(0, tk.END)
        # 添加本机节点
        self.node_listbox.insert(tk.END, f"[自己] {NODE_NAME} ({LOCAL_IP}:{FILE_SERVER_PORT})")
        # 添加其他在线节点
        for node in nodes:
            if node["ip"] != LOCAL_IP or node["port"] != FILE_SERVER_PORT:
                self.node_listbox.insert(tk.END, f"{node['name']} ({node['ip']}:{node['port']})")

    def on_node_click(self, event):
        """处理鼠标点击节点列表事件"""
        # 获取点击位置的索引
        index = self.node_listbox.nearest(event.y)
        if index >= 0:
            self.node_listbox.selection_clear(0, tk.END)
            self.node_listbox.selection_set(index)
            self.on_node_select_real()

    def on_node_select_real(self):
        """实际的节点选择处理逻辑"""
        selected_idx = self.node_listbox.curselection()
        if not selected_idx:
            # 只有当确实没有选中项时才清空选择
            if hasattr(self, 'selected_node') and self.selected_node:
                self.selected_node = None
                self.file_listbox.delete(0, tk.END)
                self.file_listbox.insert(tk.END, "请先选择一个在线节点")
                self.set_buttons_state(False)
            return

        selected_text = self.node_listbox.get(selected_idx[0])
        if "[自己]" in selected_text:
            self.selected_node = {"name": NODE_NAME, "ip": LOCAL_IP, "port": FILE_SERVER_PORT, "is_self": True}
            self._safe_log(f"已选择: [自己] {NODE_NAME}")
        else:
            # 从节点管理器的本地缓存获取节点信息（避免重复请求）
            for node in self.node_manager.online_nodes:
                if f"({node['ip']}:{node['port']})" in selected_text:
                    self.selected_node = {"name": node['name'], "ip": node['ip'], "port": node['port'], "is_self": False}
                    self._safe_log(f"已选择: {node['name']} ({node['ip']}:{node['port']})")
                    break

        self.refresh_file_list()
        self.set_buttons_state(True)

    def set_buttons_state(self, enabled):
        state = "normal" if enabled else "disabled"
        self.refresh_btn.config(state=state)
        self.upload_btn.config(state=state)
        self.download_btn.config(state="disabled")
        self.delete_btn.config(state="disabled")

    def on_file_select(self, event):
        selected_idx = self.file_listbox.curselection()
        if not selected_idx:
            self.download_btn.config(state="disabled")
            self.delete_btn.config(state="disabled")
            return

        selected_file = self.file_listbox.get(selected_idx[0])
        if selected_file in ["请先选择一个在线节点", "该节点共享文件夹为空"]:
            self.download_btn.config(state="disabled")
            self.delete_btn.config(state="disabled")
            return

        self.download_btn.config(state="normal")
        self.delete_btn.config(state="normal" if self.selected_node and self.selected_node["is_self"] else "disabled")

    def refresh_file_list(self):
        if not self.selected_node:
            return

        self.file_listbox.delete(0, tk.END)
        self.current_files = []
        try:
            if self.selected_node["is_self"]:
                # 本地节点：直接读取文件夹
                self.current_files = [f for f in os.listdir(SHARED_FOLDER) if os.path.isfile(os.path.join(SHARED_FOLDER, f))]
            else:
                # 远程节点：请求其文件服务
                resp = requests.get(f"http://{self.selected_node['ip']}:{self.selected_node['port']}/api/files", timeout=3)
                resp.raise_for_status()
                self.current_files = resp.json().get("files", [])

            if self.current_files:
                for f in self.current_files:
                    self.file_listbox.insert(tk.END, f)
                self._safe_log(f"获取 {self.selected_node['name']} 的文件列表成功 ({len(self.current_files)} 个文件)")
            else:
                self.file_listbox.insert(tk.END, "该节点共享文件夹为空")
        except Exception as e:
            error_msg = f"获取文件列表失败: {str(e)}"
            self.file_listbox.insert(tk.END, error_msg)
            self._safe_log(error_msg)

    def download_selected_file(self):
        if not self.selected_node or not self.file_listbox.curselection():
            return

        selected_file = self.file_listbox.get(self.file_listbox.curselection()[0])
        if selected_file in ["请先选择一个在线节点", "该节点共享文件夹为空"]:
            return

        save_path = os.path.join(DOWNLOAD_FOLDER, selected_file)
        try:
            if self.selected_node["is_self"]:
                import shutil
                shutil.copy2(os.path.join(SHARED_FOLDER, selected_file), save_path)
            else:
                resp = requests.get(f"http://{self.selected_node['ip']}:{self.selected_node['port']}/api/download/{selected_file}", stream=True, timeout=30)
                resp.raise_for_status()
                with open(save_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)

            self._safe_log(f"下载成功: {selected_file} → {os.path.abspath(save_path)}")
            messagebox.showinfo("成功", f"文件 '{selected_file}' 已下载到:\n{os.path.abspath(DOWNLOAD_FOLDER)}")
        except Exception as e:
            error_msg = f"下载失败: {str(e)}"
            self._safe_log(error_msg)
            messagebox.showerror("失败", error_msg)

# ... 前面的代码保持不变 ...

    def upload_file_to_node(self):
        if not self.selected_node:
            return

        file_path = filedialog.askopenfilename(title="选择要上传的文件")
        if not file_path:
            return

        filename = os.path.basename(file_path)
        try:
            with open(file_path, 'rb') as f:
                files = {'file': (filename, f)}
                resp = requests.post(f"http://{self.selected_node['ip']}:{self.selected_node['port']}/api/upload", files=files, timeout=30)
                resp.raise_for_status()

            self._safe_log(f"上传成功: {filename} → {self.selected_node['name']}")
            messagebox.showinfo("成功", f"文件 '{filename}' 已上传到 {self.selected_node['name']}")
            self.refresh_file_list()
        except Exception as e:
            error_msg = f"上传失败: {str(e)}"
            self._safe_log(error_msg)
            messagebox.showerror("失败", error_msg)

    def delete_local_file(self):
        if not self.selected_node or not self.selected_node["is_self"] or not self.file_listbox.curselection():
            return

        selected_file = self.file_listbox.get(self.file_listbox.curselection()[0])
        if not messagebox.askyesno("确认删除", f"确定要删除本地文件 '{selected_file}' 吗？"):
            return

        try:
            os.remove(os.path.join(SHARED_FOLDER, selected_file))
            self._safe_log(f"删除成功: 本地文件 '{selected_file}'")
            self.refresh_file_list()
        except Exception as e:
            error_msg = f"删除失败: {str(e)}"
            self._safe_log(error_msg)
            messagebox.showerror("失败", error_msg)

    def on_close(self):
        # 停止节点管理器线程
        if hasattr(self, 'node_manager'):
            self.node_manager.stop()
        self.destroy()

# --- 9. 主程序入口 ---
if __name__ == '__main__':
    # 启动应用
    app = AutoP2PFileShareApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    
    # 显示启动提示
    startup_msg = f"""
    🚀🚀🚀 全自动P2P文件共享工具已启动！
    • 本机IP: {LOCAL_IP}
    • 文件服务端口: {FILE_SERVER_PORT}
    • 共享文件夹: {os.path.abspath(SHARED_FOLDER)}
    • 下载文件夹: {os.path.abspath(DOWNLOAD_FOLDER)}
    
    系统已自动完成目录服务器选举，无需手动配置！
    请在其他电脑上运行相同脚本以加入共享网络。
    """
    print(startup_msg)
    messagebox.showinfo("启动成功", startup_msg.strip())
    
    app.mainloop()

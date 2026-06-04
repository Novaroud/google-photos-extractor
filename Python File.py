import json
import os
import zipfile
from datetime import datetime
import threading
import tkinter as tk
from tkinter import messagebox, filedialog
from tkinter import ttk
from tkinterdnd2 import TkinterDnD, DND_FILES
import time
import hashlib
import winsound
import sys
import shutil

try:
    import pystray
    from PIL import Image, ImageDraw
    pystray_available = True
except ImportError:
    pystray_available = False

try:
    from PIL import Image as PILImage
    from pillow_heif import register_heif_opener
    register_heif_opener()
    heif_available = True
except ImportError:
    heif_available = False

try:
    import msvcrt
except ImportError:
    msvcrt = None

APPDATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'GooglePhotosExtractor')
CONFIG_PATH = os.path.join(APPDATA_DIR, 'config.json')

processing_active = False
pause_event = threading.Event()
pause_event.set()

processed_counter = 0
skipped_counter = 0
errors_list = []

locked_files_handles = []
lock_list_mutex = threading.Lock()
extracted_md5_set = set()
tray_icon = None

total_bytes_to_process = 0
processed_bytes_counter = 0
bytes_lock = threading.Lock()
start_processing_time = 0

error_log_path = ""
speed_history = [] 

def decode_zip_path(raw_path):
    try: return raw_path.encode('cp437').decode('utf-8')
    except:
        try: return raw_path.encode('cp437').decode('cp866')
        except: return raw_path

def calculate_zip_file_md5(yin, file_path):
    md5 = hashlib.md5()
    with yin.open(file_path) as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk: break
            md5.update(chunk)
    return md5.hexdigest()

def write_exif_description(file_path, comment):
    if not comment: return
    try:
        with open(file_path, 'rb') as f:
            data = f.read()
        sos_idx = data.find(b'\xff\xda')
        if sos_idx == -1: return
        comment_bytes = comment.encode('utf-8', errors='ignore')
        comment_len = len(comment_bytes) + 2
        comment_header = b'\xff\xfe' + comment_len.to_bytes(2, byteorder='big')
        new_data = data[:2] + comment_header + comment_bytes + data[2:]
        with open(file_path, 'wb') as f:
            f.write(new_data)
    except: pass

def extract_and_fix_file(file_path, json_dict_bytes, input_zip_path, target_output_dir, settings, all_files_sizes):
    global processing_active, processed_counter, skipped_counter, errors_list, locked_files_handles, extracted_md5_set, processed_bytes_counter, speed_history
    
    decoded_file_path = decode_zip_path(file_path)
    filename = os.path.basename(decoded_file_path)
    name_part, ext_part = os.path.splitext(filename)
    
    is_heic = ext_part.lower() in ('.heic', '.heif')
    should_convert = is_heic and settings["convert_heic"] and heif_available
    
    if should_convert:
        filename = name_part + ".jpg"
        ext_part = ".jpg"

    expected_size = all_files_sizes.get(file_path, 0)

    try:
        timestamp = None
        has_json = False
        description_text = ""
        
        with zipfile.ZipFile(input_zip_path, 'r', allowZip64=True) as yin:
            old_zinfo = yin.getinfo(file_path)
            
            json_file_path = None
            possible_json_1 = os.path.basename(decoded_file_path) + ".json"
            possible_json_2 = os.path.basename(decoded_file_path) + ".supplemental-metadata.json"
            orig_name_part, _ = os.path.splitext(os.path.basename(decoded_file_path))
            
            if possible_json_1 in json_dict_bytes: json_file_path = json_dict_bytes[possible_json_1]
            elif possible_json_2 in json_dict_bytes: json_file_path = json_dict_bytes[possible_json_2]
            else:
                for j_name in json_dict_bytes.keys():
                    if j_name.startswith(orig_name_part) and j_name.endswith('.json'):
                        json_file_path = json_dict_bytes[j_name]
                        break
            
            if json_file_path:
                try:
                    json_content = json.loads(yin.read(json_file_path).decode('utf-8', errors='ignore'))
                    if 'photoTakenTime' in json_content and 'timestamp' in json_content['photoTakenTime']:
                        timestamp = int(json_content['photoTakenTime']['timestamp'])
                        has_json = True
                    if 'description' in json_content:
                        description_text = json_content['description']
                except: pass

            if not has_json:
                mode = settings["no_date_mode"]
                if mode == "skip": 
                    with bytes_lock: processed_bytes_counter += expected_size
                    return "skipped_no_date"
                elif mode == "today": timestamp = int(time.time())
                else:
                    dt = old_zinfo.date_time
                    dt_obj = datetime(dt[0], dt[1], dt[2], dt[3], dt[4], dt[5])
                    timestamp = int(dt_obj.timestamp())

        if settings["flat_structure"] == "flat":
            output_file_dir = target_output_dir
            if settings["md5_check"]:
                try:
                    with zipfile.ZipFile(input_zip_path, 'r', allowZip64=True) as yin:
                        current_md5 = calculate_zip_file_md5(yin, file_path)
                    if current_md5 in extracted_md5_set:
                        skipped_counter += 1
                        with bytes_lock: processed_bytes_counter += expected_size
                        return "skipped"
                    extracted_md5_set.add(current_md5)
                except: pass

            counter = 1
            candidate_name = filename
            while os.path.exists(os.path.join(output_file_dir, candidate_name)):
                candidate_name = f"{name_part}({counter}){ext_part}"
                counter += 1
            output_file_path = os.path.join(output_file_dir, candidate_name)
            
        elif settings["flat_structure"] == "rename_date":
            output_file_dir = target_output_dir
            formatted_date = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d_%H-%M-%S')
            candidate_name = f"{formatted_date}{ext_part}"
            counter = 1
            while os.path.exists(os.path.join(output_file_dir, candidate_name)):
                candidate_name = f"{formatted_date}_{counter}{ext_part}"
                counter += 1
            output_file_path = os.path.join(output_file_dir, candidate_name)
        else:
            output_file_path = os.path.join(target_output_dir, decoded_file_path)
            if should_convert: output_file_path = os.path.splitext(output_file_path)[0] + ".jpg"
            output_file_dir = os.path.dirname(output_file_path)

        if os.path.exists(output_file_path) and not should_convert:
            if os.path.getsize(output_file_path) == expected_size:
                skipped_counter += 1
                with bytes_lock: processed_bytes_counter += expected_size
                return "skipped"
            else:
                try: os.remove(output_file_path)
                except: pass

        os.makedirs(output_file_dir, exist_ok=True)
            
        with zipfile.ZipFile(input_zip_path, 'r', allowZip64=True) as yin:
            if should_convert:
                with yin.open(file_path) as source:
                    image = PILImage.open(source)
                    target = open(output_file_path, 'wb+')
                    if msvcrt:
                        try: msvcrt.locking(target.fileno(), msvcrt.LK_NBRLCK, 1024 * 1024 * 50)
                        except: pass
                    image.save(target, format="JPEG", quality=95)
                with bytes_lock: processed_bytes_counter += expected_size
                with lock_list_mutex: speed_history.append((time.time(), expected_size))
            else:
                target = open(output_file_path, 'wb+')
                if msvcrt:
                    try: msvcrt.locking(target.fileno(), msvcrt.LK_NBRLCK, expected_size or 1024)
                    except: pass
                with yin.open(file_path) as source:
                    while True:
                        if not processing_active:
                            target.close()
                            try: os.remove(output_file_path)
                            except: pass
                            return "stopped"
                        if not pause_event.is_set(): pause_event.wait()
                        chunk = source.read(2 * 1024 * 1024)
                        if not chunk: break
                        target.write(chunk)
                        
                        chunk_len = len(chunk)
                        with bytes_lock: processed_bytes_counter += chunk_len
                        with lock_list_mutex: speed_history.append((time.time(), chunk_len))

        if should_convert and description_text:
            write_exif_description(output_file_path, description_text)

        with lock_list_mutex:
            locked_files_handles.append((target, output_file_path, timestamp))
                        
        processed_counter += 1
        return "success"
        
    except Exception as e:
        errors_list.append(f"{filename}: {str(e)}")
        log_error_to_file(filename, str(e))
        with bytes_lock: processed_bytes_counter += expected_size
        return f"error:{str(e)}"

def log_error_to_file(filename, error_msg):
    global error_log_path
    if not error_log_path: return
    try:
        with open(error_log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] Файл: {filename} -> Ошибка: {error_msg}\n")
    except: pass

def open_error_log():
    global error_log_path
    if error_log_path and os.path.exists(error_log_path):
        os.startfile(error_log_path)
    else:
        messagebox.showinfo("Информация", "Лог пуст или ещё не был создан (ошибок пока нет).")

def release_all_file_locks():
    global locked_files_handles
    with lock_list_mutex:
        for target, path, timestamp in locked_files_handles:
            try:
                if msvcrt:
                    try:
                        target.seek(0)
                        msvcrt.locking(target.fileno(), msvcrt.LK_UNLCK, os.path.getsize(path))
                    except: pass
                target.close()
                os.utime(path, (timestamp, timestamp))
            except: pass
        locked_files_handles.clear()

def process_archive_to_folder(input_zip_path, settings, progress_callback, completion_callback, error_decision_func, current_arc_num, total_arcs, all_files_sizes, files_to_process, json_dict_bytes):
    global processing_active, processed_counter, skipped_counter, errors_list
    
    processed_counter = 0
    skipped_counter = 0
    errors_list = []
    
    base_dir = os.path.dirname(input_zip_path)
    base_name = os.path.basename(input_zip_path)
    name_without_ext, _ = os.path.splitext(base_name)
    target_output_dir = os.path.join(base_dir, name_without_ext)
    os.makedirs(target_output_dir, exist_ok=True)

    try:
        total_files = len(files_to_process)
        index = 0
        active_threads = []
        
        while (index < total_files or active_threads) and processing_active:
            if not pause_event.is_set(): pause_event.wait()
            active_threads = [t for t in active_threads if t.is_alive()]
            
            try: cores_limit = int(combo_cores.get())
            except: cores_limit = 1
            
            if len(active_threads) < cores_limit and index < total_files:
                file_path = files_to_process[index]
                current_idx = index + 1
                index += 1
                
                decoded_path = decode_zip_path(file_path)
                display_folder = "Корень" if settings["flat_structure"] != "keep" else (os.path.dirname(decoded_path) or "Root")
                display_file = os.path.basename(decoded_path)
                
                def run_task(f=file_path, idx=current_idx, folder=display_folder, fname=display_file):
                    res = extract_and_fix_file(f, json_dict_bytes, input_zip_path, target_output_dir, settings, all_files_sizes)
                    if res == "stopped": return
                    elif res in ("skipped", "skipped_no_date"):
                        progress_callback(idx, total_files, folder, fname, None, is_skipped=True, arc_info=(current_arc_num, total_arcs))
                    elif res.startswith("error:"):
                        err_msg = res.split(":", 1)[1]
                        action = error_decision_func(fname, err_msg)
                        if action == "skip":
                            progress_callback(idx, total_files, folder, fname, f"Ошибка: {err_msg}", is_skipped=False, arc_info=(current_arc_num, total_arcs))
                    else:
                        progress_callback(idx, total_files, folder, fname, None, is_skipped=False, arc_info=(current_arc_num, total_arcs))

                t = threading.Thread(target=run_task, daemon=True)
                active_threads.append(t)
                t.start()
            
            time.sleep(0.005)
            
        for t in active_threads: t.join()
        release_all_file_locks()

        if not processing_active:
            completion_callback("Error: Прервано.")
            return

        completion_callback(target_output_dir)
    except Exception as e:
        release_all_file_locks()
        completion_callback(f"Error: {str(e)}")

def load_settings():
    if not os.path.exists(CONFIG_PATH): return {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except: return {}

def save_settings():
    os.makedirs(APPDATA_DIR, exist_ok=True)
    try:
        cfg = {
            "cores": combo_cores.get(),
            "structure": combo_structure.get(),
            "no_date": combo_no_date.get(),
            "heic": var_heic.get(),
            "md5": var_md5.get(),
            "sound": var_sound.get(),
            "autoclose": var_autoclose.get()
        }
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
    except: pass

def create_tray_img():
    img = Image.new('RGB', (64, 64), '#1e1e24')
    d = ImageDraw.Draw(img)
    d.rectangle([16, 16, 48, 48], fill='#00ff66')
    return img

def show_window(icon, item):
    icon.stop()
    root.after(0, root.deiconify)

def exit_program_tray(icon, item):
    icon.stop()
    root.after(0, safe_close_app)

def hide_to_tray():
    root.withdraw()
    global tray_icon
    menu = pystray.Menu(pystray.MenuItem('Развернуть', show_window), pystray.MenuItem('Выход', exit_program_tray))
    tray_icon = pystray.Icon("gphotos_ext", create_tray_img(), "Google Photos Extractor", menu)
    threading.Thread(target=tray_icon.run, daemon=True).start()

def safe_close_app():
    global processing_active
    if processing_active:
        if not messagebox.askyesno("Внимание!", "Прямо сейчас идет обработка архивов!\nВы действительно хотите прервать процесс и выйти?"):
            return
    processing_active = False
    save_settings()
    root.destroy()
    sys.exit(0)

def set_gui_state(state):
    combo_state = "readonly" if state == "normal" else state
    button_state = "normal" if state == "normal" else state

    combo_structure.config(state=combo_state)
    combo_no_date.config(state=combo_state)
    btn_browse.config(state=button_state)
    entry_src.config(state="normal" if state == "normal" else "disabled")
    
    if heif_available:
        cb_heic.config(state=button_state)
    for child in check_frame.winfo_children():
        if child != cb_heic or heif_available:
            child.config(state=button_state)

def select_input_files():
    global selected_archives
    paths = filedialog.askopenfilenames(filetypes=[("ZIP Архивы", "*.zip")])
    if paths:
        selected_archives = list(paths)
        entry_src.delete(0, tk.END)
        entry_src.insert(0, f"Выбрано архивов: {len(selected_archives)}")

def handle_error_decision(filename, error_msg):
    global auto_action
    if auto_action is not None: return auto_action
    var_apply_all = tk.BooleanVar()
    choice = {"action": "skip"}
    dialog = tk.Toplevel(root)
    dialog.title("Ошибка файла")
    dialog.geometry("450x180")
    dialog.configure(bg="#1e1e24")
    dialog.grab_set()
    lbl = tk.Label(dialog, text=f"Ошибка в файле: {filename[:30]}...\nПричина: {error_msg[:50]}", bg="#1e1e24", fg="#ffffff")
    lbl.pack(pady=15)
    chk = tk.Checkbutton(dialog, text="Пропускать ошибки автоматически", variable=var_apply_all, bg="#1e1e24", fg="#ffffff", selectcolor="#2a2a35", activebackground="#1e1e24")
    chk.pack(pady=5)
    def select_action(act):
        choice["action"] = act
        if var_apply_all.get():
            global auto_action
            auto_action = act
        dialog.destroy()
    tk.Button(dialog, text="Пропустить", width=12, bg="#ff3333", fg="#ffffff", command=lambda: select_action("skip")).pack(pady=10)
    root.wait_window(dialog)
    return choice["action"]

def update_progress(current, total, folder, filename, err_to_log, is_skipped=False, arc_info=(1, 1)):
    short_folder = folder if len(folder) <= 35 else "..." + folder[-32:]
    short_file = filename if len(filename) <= 35 else filename[:32] + "..."
    arc_num, arc_total = arc_info
    prefix = f"[Архив {arc_num}/{arc_total}] "
    if is_skipped:
        root.after(0, lambda: label_status.config(text=f"{prefix}Пропуск/Сверка файлов ({current} из {total})...", fg="#ffb000"))
    else:
        root.after(0, lambda: label_status.config(text=f"{prefix}Извлечение: {current} из {total}", fg="#00ff66"))
        root.after(0, lambda: label_current_file.config(text=f"Файл: {short_file}"))
    root.after(0, lambda: label_current_folder.config(text=f"Текущая папка: {short_folder}"))
    root.after(0, lambda: progress_bar.config(value=current, maximum=total))

def update_time_prediction_loop():
    global processing_active, total_bytes_to_process, processed_bytes_counter, speed_history
    if not processing_active: 
        root.after(0, lambda: label_timer.config(text="Осталось: --:--:--"))
        return

    now = time.time()
    
    with lock_list_mutex:
        speed_history = [item for item in speed_history if now - item[0] <= 4.0]
        bytes_in_window = sum(item[1] for item in speed_history)

    if speed_history:
        time_span = max(0.5, now - speed_history[0][0])
        current_speed = bytes_in_window / time_span
    else:
        current_speed = 0

    with bytes_lock:
        bytes_left = max(0, total_bytes_to_process - processed_bytes_counter)

    if current_speed > 1024 and bytes_left > 0:
        seconds_left = int(bytes_left / current_speed)
        hours = seconds_left // 3600
        minutes = (seconds_left % 3600) // 60
        seconds = seconds_left % 60
        
        time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        root.after(0, lambda: label_timer.config(text=f"Осталось примерно: {time_str}", fg="#00ff66"))
    elif processed_bytes_counter >= total_bytes_to_process and total_bytes_to_process > 0:
        root.after(0, lambda: label_timer.config(text="Завершение операций...", fg="#00ff66"))
    else:
        root.after(0, lambda: label_timer.config(text="Расчёт времени...", fg="#aaaaaa"))

    if processing_active:
        root.after(1000, update_time_prediction_loop)

def toggle_pause():
    if not processing_active: return
    if pause_event.is_set():
        pause_event.clear()
        btn_pause.config(text="ПРОДОЛЖИТЬ", bg="#ffb000", fg="#1e1e24")
        label_status.config(text="ПАУЗА: Процесс заморожен", fg="#ffb000")
    else:
        pause_event.set()
        btn_pause.config(text="ПАУЗА", bg="#2a2a35", fg="#ffffff")

def start_batch_processing():
    global processing_active, auto_action, selected_archives, extracted_md5_set
    global total_bytes_to_process, processed_bytes_counter, speed_history, start_processing_time, error_log_path
    
    if processing_active:
        processing_active = False
        pause_event.set()
        label_status.config(text="Остановка и разблокировка...", fg="#ff3333")
        return

    if not selected_archives:
        messagebox.showerror("Ошибка", "Вы не выбрали ни одного ZIP-архива для обработки!")
        return

    for arc in selected_archives:
        if not os.path.exists(arc) or not arc.lower().endswith('.zip'):
            messagebox.showerror("Ошибка", f"Файл не найден или не является ZIP-архивом:\n{arc}")
            return

    try:
        target_dir = os.path.dirname(selected_archives[0])
        total_zip_size = sum(os.path.getsize(a) for a in selected_archives)
        total_b, used_b, free_b = shutil.disk_usage(target_dir)
        
        if free_b < (total_zip_size * 1.1): 
            free_mb = free_b // (1024 * 1024)
            req_mb = int((total_zip_size * 1.1) // (1024 * 1024))
            if not messagebox.askyesno("Мало места!", f"Внимание! На диске осталось около {free_mb} МБ.\nДля распаковки может потребоваться ~{req_mb} МБ.\nВы всё равно хотите продолжить?"):
                return
    except: pass

    try:
        error_log_path = os.path.join(os.path.dirname(selected_archives[0]), "extraction_errors.txt")
        with open(error_log_path, "w", encoding="utf-8") as f:
            f.write(f"=== Лог ошибок Google Photos Extractor ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===\n")
    except:
        error_log_path = ""

    auto_action = None
    processing_active = True
    extracted_md5_set.clear()
    
    total_bytes_to_process = 0
    processed_bytes_counter = 0
    speed_history.clear()
    start_processing_time = time.time()
    
    save_settings()
    set_gui_state("disabled")
    
    btn_start.config(text="ОСТАНОВИТЬ", bg="#ff3333", fg="#ffffff")
    btn_pause.config(state="normal", text="ПАУЗА", bg="#2a2a35", fg="#ffffff")
    progress_bar.pack(fill="x", padx=40, pady=5)
    label_timer.config(text="Сканирование структуры архивов...", fg="#ffb000")

    def batch_worker():
        global processing_active, total_bytes_to_process
        
        date_mapping = {"Взять дату из ZIP": "zip", "Поставить текущую дату": "today", "Пропустить файл": "skip"}
        structure_mapping = {
            "Сохранить структуру папок": "keep", 
            "Все файлы в одну общую папку": "flat",
            "Переименовать по дате съемки": "rename_date"
        }
        settings = {
            "flat_structure": structure_mapping.get(combo_structure.get(), "keep"),
            "no_date_mode": date_mapping.get(combo_no_date.get(), "zip"),
            "convert_heic": var_heic.get(),
            "md5_check": var_md5.get()
        }
        
        archives_data = []
        for arc_path in selected_archives:
            if not processing_active: break
            try:
                with zipfile.ZipFile(arc_path, 'r', allowZip64=True) as yin:
                    all_files = yin.namelist()
                    files_to_process = [f for f in all_files if not f.endswith('.json') and not f.endswith('/')]
                    all_files_sizes = {f.filename: f.file_size for f in yin.infolist()}
                    json_dict_bytes = {os.path.basename(decode_zip_path(f)): f for f in all_files if f.endswith('.json')}
                    
                    for f in files_to_process:
                        total_bytes_to_process += all_files_sizes.get(f, 0)
                        
                    archives_data.append((arc_path, all_files_sizes, files_to_process, json_dict_bytes))
            except Exception as e:
                processing_active = False
                root.after(0, lambda err=str(e): label_status.config(text=f"Error анализа: {err}", fg="#ff3333"))
                return

        root.after(0, update_time_prediction_loop)

        total_arcs = len(archives_data)
        last_dir = None

        for idx, (arc_path, all_files_sizes, files_to_process, json_dict_bytes) in enumerate(archives_data, start=1):
            if not processing_active: break
            archive_done = threading.Event()
            
            def item_complete(res_dir):
                nonlocal last_dir; last_dir = res_dir; archive_done.set()
                
            root.after(0, lambda path=arc_path: label_current_folder.config(text=f"Распаковка: {os.path.basename(path)}"))
            
            threading.Thread(target=process_archive_to_folder, 
                             args=(arc_path, settings, update_progress, item_complete, handle_error_decision, idx, total_arcs, all_files_sizes, files_to_process, json_dict_bytes), 
                             daemon=True).start()
            archive_done.wait()

        processing_active = False
        root.after(0, lambda: progress_bar.pack_forget())
        root.after(0, lambda: btn_start.config(text="СТАРТ", bg="#00ff66", fg="#1e1e24"))
        root.after(0, lambda: btn_pause.config(state="disabled", text="ПАУЗА", bg="#2a2a35", fg="#777777"))
        root.after(0, lambda: set_gui_state("normal")) 
        root.after(0, lambda: label_timer.config(text="Осталось: --:--:--", fg="#777777"))
        
        if last_dir and not last_dir.startswith("Error:"):
            root.after(0, lambda: label_status.config(text="Все архивы успешно обработаны!", fg="#00ff66"))
            if var_sound.get(): winsound.MessageBeep(winsound.MB_ICONASTERISK)
            if tray_icon: tray_icon.notify("Все архивы успешно извлечены!", "Google Photos Extractor")
            os.startfile(os.path.dirname(last_dir))
            if var_autoclose.get(): root.after(1000, root.destroy)
        else:
            err_text = last_dir if (last_dir and last_dir.startswith("Error:")) else "Обработка завершена или прервана."
            root.after(0, lambda: label_status.config(text=err_text, fg="#ff3333"))

    threading.Thread(target=batch_worker, daemon=True).start()

def on_drop(event):
    if processing_active: return
    global selected_archives
    raw_data = event.data.strip()
    paths = [p.strip('{}') for p in raw_data.split('} {')] if raw_data.startswith('{') else raw_data.split()
    zips = [p for p in paths if p.lower().endswith('.zip') and os.path.exists(p)]
    if zips:
        selected_archives = zips
        entry_src.delete(0, tk.END)
        entry_src.insert(0, f"Выбрано архивов: {len(selected_archives)}")
    else: messagebox.showerror("Ошибка", "Перетащите корректные файлы .zip!")

if __name__ == '__main__':
    try:
        root = TkinterDnD.Tk()
    except:
        root = tk.Tk()
        
    root.title("Google Photos Extractor")
    root.geometry("560x515")
    root.configure(bg="#1e1e24")
    root.resizable(False, False)
    
    root.protocol('WM_DELETE_WINDOW', safe_close_app)

    style = ttk.Style()
    style.theme_use('default')
    style.configure("TCombobox", fieldbackground="#151518", background="#2a2a35", foreground="#ffffff", arrowcolor="#00ff66", bordercolor="#151518")
    style.map("TCombobox", fieldbackground=[('readonly', '#151518')], foreground=[('readonly', '#ffffff')])
    style.configure("TProgressbar", thickness=12, background="#00ff66", troughcolor="#2a2a35", bordercolor="#1e1e24")

    saved_cfg = load_settings()

    opt_frame = tk.LabelFrame(root, text=" Конфигурация параметров ", bg="#1e1e24", fg="#aaaaaa", font=("Arial", 9, "bold"), bd=1, relief="solid")
    opt_frame.pack(fill="x", padx=40, pady=(20, 10))
    opt_frame.grid_columnconfigure((0, 1), weight=1)

    tk.Label(opt_frame, text="Нагрузка на процессор (Ядра):", bg="#1e1e24", fg="#00ff66", font=("Arial", 9, "bold")).grid(row=0, column=0, padx=15, pady=(10, 2), sticky="w")
    combo_cores = ttk.Combobox(opt_frame, values=["1", "2", "3", "4"], state="readonly", width=25)
    combo_cores.set(saved_cfg.get("cores", "1"))
    combo_cores.grid(row=1, column=0, padx=15, pady=(0, 10), sticky="w")

    tk.Label(opt_frame, text="Организация файлов на диске:", bg="#1e1e24", fg="#00ff66", font=("Arial", 9, "bold")).grid(row=0, column=1, padx=15, pady=(10, 2), sticky="w")
    combo_structure = ttk.Combobox(opt_frame, values=["Сохранить структуру папок", "Все файлы в одну общую папку", "Переименовать по дате съемки"], state="readonly", width=28)
    combo_structure.set(saved_cfg.get("structure", "Сохранить структуру папок"))
    combo_structure.grid(row=1, column=1, padx=15, pady=(0, 10), sticky="w")

    tk.Label(opt_frame, text="Если в JSON файле нет даты:", bg="#1e1e24", fg="#00ff66", font=("Arial", 9, "bold")).grid(row=2, column=0, padx=15, pady=(5, 2), sticky="w")
    combo_no_date = ttk.Combobox(opt_frame, values=["Взять дату из ZIP", "Поставить текущую дату", "Пропустить файл"], state="readonly", width=25)
    combo_no_date.set(saved_cfg.get("no_date", "Взять дату из ZIP"))
    combo_no_date.grid(row=3, column=0, padx=15, pady=(0, 15), sticky="w")

    if pystray_available:
        tk.Button(opt_frame, text="Свернуть в трей", font=("Arial", 8, "bold"), bg="#2a2a35", fg="#ffffff", bd=0, command=hide_to_tray).grid(row=3, column=1, padx=15, pady=(0,15), sticky="e")

    check_frame = tk.Frame(root, bg="#1e1e24")
    check_frame.pack(fill="x", padx=40, pady=5)
    
    var_heic = tk.BooleanVar(value=saved_cfg.get("heic", heif_available))
    cb_heic = tk.Checkbutton(check_frame, text="Конвертировать HEIC / HEIF файлы в JPG", variable=var_heic, bg="#1e1e24", fg="#ffffff", selectcolor="#2a2a35", activebackground="#1e1e24", font=("Arial", 9))
    if heif_available:
        cb_heic.pack(anchor="w")
    
    var_md5 = tk.BooleanVar(value=saved_cfg.get("md5", True))
    tk.Checkbutton(check_frame, text="Умное удаление дубликатов по хэшу MD5", variable=var_md5, bg="#1e1e24", fg="#ffffff", selectcolor="#2a2a35", activebackground="#1e1e24", font=("Arial", 9)).pack(anchor="w", pady=2)

    var_sound = tk.BooleanVar(value=saved_cfg.get("sound", True))
    tk.Checkbutton(check_frame, text="Звуковой сигнал по завершении", variable=var_sound, bg="#1e1e24", fg="#ffffff", selectcolor="#2a2a35", activebackground="#1e1e24", font=("Arial", 9)).pack(anchor="w")
    
    var_autoclose = tk.BooleanVar(value=saved_cfg.get("autoclose", False))
    tk.Checkbutton(check_frame, text="Автовыход из программы по финишу", variable=var_autoclose, bg="#1e1e24", fg="#ffffff", selectcolor="#2a2a35", activebackground="#1e1e24", font=("Arial", 9)).pack(anchor="w", pady=2)

    path_frame = tk.Frame(root, bg="#1e1e24")
    path_frame.pack(fill="x", padx=40, pady=10)
    tk.Label(path_frame, text="Выбранные архивы Google Фото:", bg="#1e1e24", fg="#aaaaaa", font=("Arial", 9, "bold")).pack(anchor="w")
    src_subframe = tk.Frame(path_frame, bg="#1e1e24")
    src_subframe.pack(fill="x", pady=2)
    entry_src = tk.Entry(src_subframe, bg="#151518", fg="#ffffff", bd=1, relief="solid", font=("Arial", 10))
    entry_src.pack(side="left", fill="x", expand=True, ipady=4)
    btn_browse = tk.Button(src_subframe, text="Обзор...", bg="#2a2a35", fg="#ffffff", command=select_input_files)
    btn_browse.pack(side="right", padx=5)

    btn_box = tk.Frame(root, bg="#1e1e24")
    btn_box.pack(fill="x", padx=40, pady=(5, 10))
    btn_start = tk.Button(btn_box, text="СТАРТ", font=("Arial", 11, "bold"), bg="#00ff66", fg="#1e1e24", bd=0, cursor="hand2", width=25, command=start_batch_processing)
    btn_start.pack(side="left", fill="x", expand=True, ipady=6)
    btn_pause = tk.Button(btn_box, text="ПАУЗА", font=("Arial", 11, "bold"), bg="#2a2a35", fg="#777777", bd=0, cursor="hand2", state="disabled", width=15, command=toggle_pause)
    btn_pause.pack(side="right", padx=(10, 0), ipady=6)

    progress_bar = ttk.Progressbar(root, mode="determinate")

    status_frame = tk.Frame(root, bg="#1e1e24")
    status_frame.pack(fill="x", padx=40, pady=5)
    
    label_timer = tk.Label(status_frame, text="Осталось: --:--:--", font=("Arial", 10, "bold"), bg="#1e1e24", fg="#aaaaaa")
    label_timer.pack(anchor="w", pady=(0, 2))

    label_status = tk.Label(status_frame, text="Готов к работе.", font=("Arial", 10, "bold"), bg="#1e1e24", fg="#777777")
    label_status.pack(anchor="w")
    label_current_folder = tk.Label(status_frame, text="Текущая папка: —", font=("Consolas", 9), bg="#1e1e24", fg="#aaaaaa")
    label_current_folder.pack(anchor="w", pady=2)
    label_current_file = tk.Label(status_frame, text="Файл: —", font=("Consolas", 9), bg="#1e1e24", fg="#00ff66")
    label_current_file.pack(anchor="w")

    btn_open_log = tk.Button(status_frame, text="📄 Открыть лог ошибок (extraction_errors.txt)", font=("Arial", 8, "bold"), bg="#2a2a35", fg="#ffffff", bd=0, cursor="hand2", padx=10, pady=4, command=open_error_log)
    btn_open_log.pack(anchor="w", pady=(8, 0))

    try:
        root.drop_target_register(DND_FILES)
        root.dnd_bind('<<Drop>>', on_drop)
    except:
        pass

    root.mainloop()
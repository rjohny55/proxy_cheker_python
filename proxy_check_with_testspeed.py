#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import threading
import subprocess
import re
import platform
import queue
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# --- Попытка импорта зависимостей ---
try:
    import requests
    import urllib3
    from colorama import init, Fore, Style
    # Отключаем предупреждения SSL
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("Ошибка: Не найдены библиотеки requests или colorama.")
    print("Выполните: pip install requests colorama")
    sys.exit(1)

# --- Константы ---
CONFIG_FILE = Path("config.json")
DEFAULT_CONFIG = {
    "threads": 100,
    "timeout": 10,              # Таймаут соединения (сек)
    "max_ms": 5000,             # Макс. задержка (мс) для сохранения
    "import_files": ["proxies.txt"],
    "export_file": "good_proxies.txt",
    "host_check_url": "https://www.google.com",
    "ip_check_url": "https://api.ipify.org?format=json",
    # Пинг
    "enable_ping": True,
    "ping_timeout_ms": 1000,
    # Тест скорости
    "enable_speed_test": False,
    "speed_test_url": "http://speedtest.tele2.net/1MB.zip",
    "speed_min_good_kbps": 100, # Просто для цветного выделения
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# --- Глобальные переменные ---
stats = {
    "checked": 0,
    "good": 0,
    "total": 0
}
stats_lock = threading.Lock()
ping_command_available = False
result_queue = queue.Queue()

# --- Функции конфигурации и вывода ---

def load_or_create_config():
    """Загружает конфиг, дополняя отсутствующие ключи."""
    if not CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
        except Exception as e:
            print(f"Ошибка создания конфига: {e}")
            return DEFAULT_CONFIG

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
            config = DEFAULT_CONFIG.copy()
            config.update(loaded)
            return config
    except Exception:
        return DEFAULT_CONFIG

def print_banner(config):
    """Выводит баннер и настройки."""
    print(Fore.GREEN + r'''
___________ _______   ____   __  _____  _   _  _____ _____  _   __ ___________
| ___ \ ___ \  _  \ \ / /\ \ / / /  __ \| | | ||  ___/  __ \| | / /|  ___| ___ \
| |_/ / |_/ / | | |\ V /  \ V /  | /  \/| |_| || |__ | /  \/| |/ / | |__ | |_/ /
|  __/|    /| | | |/   \   \ /   | |    |  _  ||  __|| |    |    \ |  __||    /
| |   | |\ \\ \_/ / /^\ \  | |   | \__/\| | | || |___| \__/\| |\  \| |___| |\ \
\_|   \_| \_|\___/\/   \/  \_/    \____/\_| |_/\____/ \____/\_| \_/\____/\_| \_|
''' + Style.RESET_ALL)
    
    print(Fore.CYAN + "--- Настройки (из config.json) ---")
    print(f"    Потоки: {config['threads']}")
    print(f"    Тайм-аут: {config['timeout']} сек")
    print(f"    Макс. пинг (HTTP): {config['max_ms']} мс")
    print(f"    Импорт: {config['import_files']}")
    print(f"    Экспорт: {config['export_file']}")
    print(f"    Пинг (ICMP): {'Вкл' if config['enable_ping'] else 'Выкл'}")
    print(f"    Тест скорости: {'Вкл' if config['enable_speed_test'] else 'Выкл'}")
    if config['enable_speed_test']:
        print(f"      URL: {config['speed_test_url']}")
    print("-" * 40 + Style.RESET_ALL)

# --- Сетевые функции ---

def check_ping_availability():
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    try:
        subprocess.run(['ping', param, '1', '127.0.0.1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        return True
    except:
        return False

def system_ping(ip, timeout_ms):
    if not ping_command_available: return None
    try:
        timeout_sec = timeout_ms / 1000.0
        param_n = '-n' if platform.system().lower() == 'windows' else '-c'
        param_w = '-w' if platform.system().lower() == 'windows' else '-W'
        timeout_val = str(timeout_ms) if platform.system().lower() == 'windows' else str(timeout_sec)
        
        cmd = ['ping', param_n, '1', param_w, timeout_val, ip]
        
        # Скрываем окно на Windows
        startupinfo = None
        if platform.system().lower() == 'windows':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 0.5, startupinfo=startupinfo)
        if res.returncode == 0:
            match = re.search(r"time[=<]([\d.]+)\s?ms", res.stdout, re.IGNORECASE)
            return int(float(match.group(1))) if match else 1
    except:
        pass
    return None

def test_download_speed(proxies, url, timeout):
    """Тестирует скорость скачивания (KB/s)."""
    try:
        start_time = time.time()
        bytes_downloaded = 0
        with requests.get(url, proxies=proxies, stream=True, timeout=timeout, verify=False) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=8192):
                bytes_downloaded += len(chunk)
                # Если качаем дольше таймаута — прерываем
                if time.time() - start_time > timeout:
                    break
        
        duration = time.time() - start_time
        if duration < 0.01: duration = 0.01 # Защита от деления на 0
        
        speed_kbps = (bytes_downloaded / 1024) / duration
        return int(speed_kbps)
    except:
        return None

def parse_proxy(proxy_str):
    """Универсальный парсер прокси."""
    proxy_str = proxy_str.strip()
    if '://' in proxy_str: proxy_str = proxy_str.split('://')[-1]
    
    parts = proxy_str.split(':')
    # ip:port
    if len(parts) == 2:
        return {"http": f"http://{proxy_str}", "https": f"http://{proxy_str}", "raw": proxy_str, "ip": parts[0]}
    # ip:port:user:pass -> user:pass@ip:port
    elif len(parts) == 4:
        formatted = f"{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        return {"http": f"http://{formatted}", "https": f"http://{formatted}", "raw": proxy_str, "ip": parts[0]}
    # user:pass@ip:port
    elif '@' in proxy_str:
        return {"http": f"http://{proxy_str}", "https": f"http://{proxy_str}", "raw": proxy_str, "ip": proxy_str.split('@')[1].split(':')[0]}
    return None

# --- Поток записи ---

def file_writer_worker(filepath):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            while True:
                item = result_queue.get()
                if item is None: break
                f.write(item + '\n')
                f.flush()
                result_queue.task_done()
    except Exception as e:
        print(f"Ошибка записи: {e}")

# --- Основная логика ---

def check_single_proxy(proxy_raw, config):
    proxy_data = parse_proxy(proxy_raw)
    if not proxy_data: return

    proxy_dict = {"http": proxy_data['http'], "https": proxy_data['https']}
    headers = {'User-Agent': config['user_agent']}
    
    start_time = time.perf_counter()
    latency = None
    ping_val = None
    speed_val = None
    is_alive = False
    
    try:
        # 1. Проверка HTTP (Latency)
        with requests.get(config['host_check_url'], proxies=proxy_dict, timeout=config['timeout'], stream=True, verify=False, headers=headers) as r:
            if 200 <= r.status_code < 400:
                is_alive = True
        
        latency = int((time.perf_counter() - start_time) * 1000)

        # 2. Дополнительные тесты, если прокси жив
        if is_alive:
            # Пинг
            if config['enable_ping'] and ping_command_available:
                ping_val = system_ping(proxy_data['ip'], config['ping_timeout_ms'])
            
            # Скорость
            if config['enable_speed_test']:
                speed_val = test_download_speed(proxy_dict, config['speed_test_url'], config['timeout'])

    except:
        pass

    # --- Сборка статуса ---
    log_parts = []
    status_color = Fore.RED
    
    if is_alive:
        if latency <= config['max_ms']:
            status_color = Fore.GREEN
            log_parts.append(f"HTTP: {latency}ms")
            
            if ping_val is not None:
                log_parts.append(f"Ping: {ping_val}ms")
            
            if speed_val is not None:
                sp_color = Fore.GREEN if speed_val >= config['speed_min_good_kbps'] else Fore.YELLOW
                log_parts.append(f"Speed: {sp_color}{speed_val} KB/s{Style.RESET_ALL}")
            elif config['enable_speed_test']:
                log_parts.append("Speed: Fail")

            # Пишем в файл
            result_queue.put(proxy_data['raw'])
            with stats_lock: stats['good'] += 1
        else:
            status_color = Fore.YELLOW
            log_parts.append(f"Slow: {latency}ms")
    else:
        log_parts.append("Dead")

    # Вывод в консоль
    msg = " | ".join(log_parts)
    display_proxy = (proxy_data['raw'][:25] + '..') if len(proxy_data['raw']) > 25 else proxy_data['raw']
    print(f"{Fore.WHITE}{display_proxy:<30} {status_color}{msg}{Style.RESET_ALL}")

    # Обновление заголовка
    with stats_lock:
        stats['checked'] += 1
        title = f"Checker | {stats['checked']}/{stats['total']} | Good: {stats['good']}"
    
    if platform.system() == 'Windows':
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    else:
        sys.stdout.write(f"\x1b]2;{title}\x07")
        sys.stdout.flush()

# --- Main ---

def main():
    init()
    os.system('cls' if os.name == 'nt' else 'clear')
    
    config = load_or_create_config()
    print_banner(config)
    
    global ping_command_available
    ping_command_available = check_ping_availability()
    
    # Загрузка прокси
    proxies = set()
    for fname in config['import_files']:
        path = Path(fname)
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip() and ':' in line: proxies.add(line.strip())
        else:
            print(f"{Fore.YELLOW}Файл {fname} не найден{Style.RESET_ALL}")
    
    stats['total'] = len(proxies)
    if stats['total'] == 0:
        print(f"{Fore.RED}Прокси не найдены.{Style.RESET_ALL}")
        return

    # Запуск писателя
    writer = threading.Thread(target=file_writer_worker, args=(config['export_file'],), daemon=True)
    writer.start()

    # Запуск воркеров
    print(f"{Fore.CYAN}Начинаю проверку...{Style.RESET_ALL}")
    try:
        with ThreadPoolExecutor(max_workers=config['threads']) as ex:
            futures = [ex.submit(check_single_proxy, p, config) for p in list(proxies)]
            for f in futures: f.result()
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Прервано...{Style.RESET_ALL}")
    
    result_queue.put(None)
    writer.join()

    print("\n" + "="*40)
    print(f"{Fore.GREEN}Готово. Рабочих: {stats['good']}{Style.RESET_ALL}")
    input("Enter для выхода...")

if __name__ == "__main__":
    main()

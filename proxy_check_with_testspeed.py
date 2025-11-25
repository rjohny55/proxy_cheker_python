#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import json
import time
import subprocess
import re
import platform
import ipaddress
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Импорты с проверкой ---
try:
    import requests
    import urllib3
    from colorama import init, Fore, Style
    from tqdm import tqdm
    
    # Отключаем SSL предупреждения
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError as e:
    print(f"Ошибка: Не найдены необходимые библиотеки ({e})")
    print('Выполните: pip install "requests[socks]" colorama tqdm')
    sys.exit(1)

# --- Настройки ---
CONFIG_FILE = Path("config.json")
DEFAULT_CONFIG = {
    "threads": 100,             # Количество потоков
    "timeout": 10,              # Таймаут (сек)
    "max_ms": 3000,             # Макс. latency
    "import_files": ["proxies.txt"],
    "export_file": "good_proxies.txt",
    
    # Проверка доступности (Ping/Latency)
    "host_check_url": "https://www.google.com",
    
    # Проверка анонимности (Сверка IP)
    "check_anonymity": False,
    "anonymity_url": "https://httpbin.org/ip", # Или https://api.ipify.org?format=json
    
    "verify_ssl": False,        
    "enable_ping": True,        
    "ping_timeout_ms": 1000,
    "enable_speed_test": False, 
    "speed_test_url": "http://speedtest.tele2.net/1MB.zip",
    "speed_limit_bytes": 524288, 
    "allow_private_ips": False, 
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# --- Интерфейс и Конфигурация ---

def print_banner():
    print(Fore.CYAN + r'''
______                        _____  _                  _             
| ___ \                      /  __ \| |                | |            
| |_/ /_ __ _____  ___   _   | /  \/| |__   ___   ___  | | __ ___ _ __
|  __/| '__/ _ \ \/ / | | |  | |    | '_ \ / _ \ / __| | |/ // _ \ '__|
| |   | | | (_) >  <| |_| |  | \__/\| | | |  __/| (__  |   <|  __/ |  
\_|   |_|  \___/_/\_\\__, |   \____/|_| |_|\___| \___| |_|\_\\___|_|  
                      __/ |            v6.0 (Socks Edition)           
                     |___/                                            
''' + Style.RESET_ALL)

def print_settings(config, ping_avail):
    print(Fore.YELLOW + "--- Настройки ---" + Style.RESET_ALL)
    print(f" Потоки:        {Fore.GREEN}{config['threads']}{Style.RESET_ALL}")
    print(f" Таймаут:       {Fore.GREEN}{config['timeout']}s{Style.RESET_ALL}")
    print(f" Host Check:    {config['host_check_url']}")
    
    anon_status = f"{Fore.GREEN}ВКЛ{Style.RESET_ALL}" if config['check_anonymity'] else f"{Fore.RED}ВЫКЛ{Style.RESET_ALL}"
    print(f" Anonymity:     {anon_status} ({config['anonymity_url']})")
    
    ping_status = f"{Fore.GREEN}ВКЛ{Style.RESET_ALL}" if (config['enable_ping'] and ping_avail) else f"{Fore.RED}ВЫКЛ{Style.RESET_ALL}"
    print(f" ICMP Ping:     {ping_status}")
    
    speed_status = f"{Fore.GREEN}ВКЛ{Style.RESET_ALL}" if config['enable_speed_test'] else f"{Fore.RED}ВЫКЛ{Style.RESET_ALL}"
    print(f" Speed Test:    {speed_status}")
    print("-" * 50)

def load_config():
    if not CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
        except: pass
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            c = DEFAULT_CONFIG.copy()
            c.update(json.load(f))
            return c
    except: return DEFAULT_CONFIG

# --- Сетевые функции ---

def check_ping_tool():
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    try:
        subprocess.run(['ping', param, '1', '127.0.0.1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        return True
    except: return False

def system_ping(ip, timeout_ms):
    try:
        timeout_sec = timeout_ms / 1000.0
        if platform.system().lower() == 'windows':
            cmd = ['ping', '-n', '1', '-w', str(timeout_ms), ip]
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs = {'startupinfo': si, 'creationflags': subprocess.CREATE_NO_WINDOW}
        else:
            cmd = ['ping', '-c', '1', '-W', str(timeout_sec), ip]
            kwargs = {}
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 0.5, **kwargs)
        if res.returncode == 0:
            match = re.search(r"time[=<]([\d.]+)\s?ms", res.stdout, re.IGNORECASE)
            return int(float(match.group(1))) if match else 1
    except: pass
    return None

def check_speed(proxies, url, limit, timeout, verify):
    try:
        start = time.time()
        downloaded = 0
        with requests.get(url, proxies=proxies, stream=True, timeout=timeout, verify=verify) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=8192):
                downloaded += len(chunk)
                if downloaded >= limit or (time.time() - start) > timeout:
                    break
        duration = time.time() - start
        if duration < 0.01: duration = 0.01
        return int((downloaded / 1024) / duration)
    except: return None

def parse_proxy(proxy_str):
    raw = proxy_str.strip()
    if not raw: return None
    scheme = "http"
    body = raw
    if "://" in raw:
        scheme, body = raw.split("://", 1)
        scheme = scheme.lower()

    if '@' in body:
        formatted_body = body
        try:
            ip = body.split('@')[1].split(':')[0]
        except: return None
    else:
        parts = body.split(':')
        if len(parts) == 2:
            ip, port = parts
            formatted_body = f"{ip}:{port}"
        elif len(parts) == 4:
            ip, port, user, pwd = parts
            formatted_body = f"{user}:{pwd}@{ip}:{port}"
        else: return None

    is_private = False
    try:
        if ipaddress.ip_address(ip).is_private: is_private = True
    except: pass

    full_url = f"{scheme}://{formatted_body}"
    return {
        "proxies": {"http": full_url, "https": full_url},
        "ip": ip,
        "raw": raw,
        "is_private": is_private
    }

# --- Воркер ---

def check_single_proxy(proxy_str, config, ping_available):
    data = parse_proxy(proxy_str)
    if not data: return None
    if data['is_private'] and not config['allow_private_ips']: return None

    result = {'proxy': data['raw'], 'latency': 0, 'ping': None, 'speed': None, 'anon': False}

    # 1. ICMP Ping (Независимый)
    if config['enable_ping'] and ping_available:
        result['ping'] = system_ping(data['ip'], config['ping_timeout_ms'])

    # 2. HTTP Host Check (Базовая проверка жизни)
    try:
        with requests.get(
            config['host_check_url'],
            proxies=data['proxies'],
            timeout=config['timeout'],
            headers={'User-Agent': config['user_agent']},
            verify=config['verify_ssl'],
            stream=True 
        ) as r:
            result['latency'] = int(r.elapsed.total_seconds() * 1000)
            if r.status_code >= 400: return None
    except Exception: return None

    if result['latency'] > config['max_ms']: return None

    # 3. Anonymity Check (Опционально)
    if config['check_anonymity']:
        try:
            # Делаем запрос к чекеру IP (httpbin.org/ip и т.д.)
            with requests.get(
                config['anonymity_url'],
                proxies=data['proxies'],
                timeout=config['timeout'],
                verify=config['verify_ssl']
            ) as r:
                if r.status_code == 200:
                    # Проверяем, что в ответе есть IP прокси. 
                    # Это значит, что трафик прошел через него.
                    if data['ip'] in r.text:
                        result['anon'] = True
                    else:
                        # Если IP прокси нет в ответе, возможно это прозрачный прокси 
                        # или ротируемый (выходной IP другой). 
                        # Если строгая проверка нужна - отбрасываем:
                        return None 
                else:
                    return None
        except Exception: return None

    # 4. Speed Test
    if config['enable_speed_test']:
        result['speed'] = check_speed(data['proxies'], config['speed_test_url'], config['speed_limit_bytes'], config['timeout'], config['verify_ssl'])

    return result

# --- Main ---

def main():
    init()
    os.system('cls' if os.name == 'nt' else 'clear')
    
    config = load_config()
    print_banner()
    
    ping_avail = check_ping_tool()
    print_settings(config, ping_avail)
    
    # Загрузка
    proxies = set()
    for fname in config['import_files']:
        path = Path(fname)
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip(): proxies.add(line.strip())
        else:
            print(f"{Fore.RED}Файл {fname} не найден!{Style.RESET_ALL}")
    
    proxies_list = list(proxies)
    total = len(proxies_list)
    
    if total == 0:
        print(Fore.RED + "Нет прокси для проверки. Добавьте их в proxies.txt" + Style.RESET_ALL)
        input("Enter для выхода...")
        sys.exit()

    print(f"Загружено уникальных прокси: {total}")
    print(f"Результаты будут сохранены в: {Fore.YELLOW}{config['export_file']}{Style.RESET_ALL}")
    input(f"Нажмите {Fore.GREEN}Enter{Style.RESET_ALL} для старта...")
    print("-" * 50)

    # Работа
    good_count = 0
    with open(config['export_file'], 'w', encoding='utf-8') as f_export:
        with ThreadPoolExecutor(max_workers=config['threads']) as executor:
            future_to_proxy = {executor.submit(check_single_proxy, p, config, ping_avail): p for p in proxies_list}
            
            pbar = tqdm(total=total, unit="prx", ncols=95, bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt} Good: {postfix}]")
            pbar.postfix = 0
            
            for future in as_completed(future_to_proxy):
                try:
                    res = future.result()
                    if res:
                        good_count += 1
                        pbar.postfix = good_count
                        f_export.write(res['proxy'] + '\n')
                        f_export.flush()
                        
                        info = [f"{res['latency']}ms"]
                        if res['anon']: info.append("Anon:OK")
                        if res['ping']: info.append(f"Ping:{res['ping']}")
                        if res['speed']: info.append(f"Spd:{res['speed']}KB/s")
                        
                        status_str = f"{Fore.GREEN}OK ({'|'.join(info)}){Style.RESET_ALL}"
                        proxy_display = (res['proxy'][:30] + '..') if len(res['proxy']) > 30 else res['proxy']
                        tqdm.write(f"{proxy_display:<33} {status_str}")
                except KeyboardInterrupt:
                    executor.shutdown(wait=False)
                    break
                except Exception: pass
                finally: pbar.update(1)
            pbar.close()

    print("\n" + "="*50)
    print(f"{Fore.GREEN}Проверка завершена!{Style.RESET_ALL}")
    print(f"Всего рабочих: {Fore.GREEN}{good_count}{Style.RESET_ALL}")
    
    if platform.system() == 'Windows':
        os.system("pause")

if __name__ == "__main__":
    main()

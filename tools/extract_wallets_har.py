import base64
import json
import re


def is_solana_address(address):
    # Solana 地址长度通常在 32-44 位，使用 Base58 字符集
    if not (32 <= len(address) <= 44):
        return False
    if not re.match(r'^[1-9A-HJ-NP-Za-km-z]+$', address):
        return False
    if address == "So11111111111111111111111111111111111111111":
        return False
    return True


def extract_from_json(obj, found_addresses):
    if isinstance(obj, str):
        if is_solana_address(obj):
            found_addresses.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            extract_from_json(v, found_addresses)
    elif isinstance(obj, list):
        for item in obj:
            extract_from_json(item, found_addresses)


def main():
    wallets = set()
    har_file = 'gmgn.ai.har'  # 请确保文件名正确

    with open(har_file, 'r', encoding='utf-8') as f:
        har_data = json.load(f)

    for entry in har_data['log']['entries']:
        content = entry['response'].get('content', {})
        text = content.get('text', '')
        encoding = content.get('encoding', '')
        mime_type = content.get('mimeType', '')

        if not text:
            continue

        # 核心：处理 HAR 中的 Base64 编码内容
        if encoding == 'base64':
            try:
                text = base64.b64decode(text).decode('utf-8', errors='ignore')
            except:
                continue

        # 仅解析 JSON 响应，避开 JS/HTML 中的干扰
        if 'application/json' in mime_type:
            try:
                data = json.loads(text)
                extract_from_json(data, wallets)
            except:
                pass

        # 同时也检查请求的 URL（有时地址在 URL 路径中）
        url = entry['request']['url']
        path_matches = re.findall(r'/([1-9A-HJ-NP-Za-km-z]{32,44})(?:[/?]|$)', url)
        for m in path_matches:
            if is_solana_address(m):
                wallets.add(m)

    # 排序并保存
    sorted_wallets = sorted(list(wallets))
    with open('wallets.txt', 'w', encoding='utf-8') as f:
        for w in sorted_wallets:
            f.write(w + '\n')

    print(f"提取完成！共找到 {len(wallets)} 个 Solana 地址，已保存至 wallets.txt")


if __name__ == "__main__":
    main()

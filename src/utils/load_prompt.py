def load_prompt_from_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read().strip()  # .strip() 移除首尾空白字符
import ast
import sys

def verify():
    with open('nexus/core/pipeline.py', 'r', encoding='utf-8') as f:
        src = f.read()

    tree = ast.parse(src)
    loop_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == '_daily_reset_loop':
            loop_node = node
            break

    if not loop_node:
        print("ERROR: _daily_reset_loop is missing!")
        sys.exit(1)

    code_text = ast.get_source_segment(src, loop_node)
    
    if "date.today()" in code_text:
        print("ERROR: The method still contains date.today(), bug not fixed.")
        sys.exit(1)

    if "seconds_until_midnight <= 0" not in code_text:
        print("ERROR: The safety guard `seconds_until_midnight <= 0` is missing.")
        sys.exit(1)
        
    print("SUCCESS: _daily_reset_loop logic is fixed and safe.")
    sys.exit(0)

if __name__ == '__main__':
    verify()

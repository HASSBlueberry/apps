import ast
import hashlib
import re

def call_literals(text, name):
    """Read literal addCfg/addROD arguments without eval or executing gateway JS."""
    for match in re.finditer(r'\b' + re.escape(name) + r'\s*\(', text):
        start, position, depth, quote_char, escape = match.end(), match.end(), 1, None, False
        while position < len(text) and depth:
            char = text[position]
            if quote_char:
                if escape: escape = False
                elif char == '\\': escape = True
                elif char == quote_char: quote_char = None
            elif char in ('"', "'"): quote_char = char
            elif char == '(': depth += 1
            elif char == ')': depth -= 1
            position += 1
        if depth: continue
        material = text[start:position - 1]
        try:
            tree = ast.parse('[' + material + ']', mode='eval')
            class Constants(ast.NodeTransformer):
                def visit_Dict(self, node):
                    node.keys = [ast.copy_location(ast.Constant(key.id),key) if isinstance(key,ast.Name) else key for key in node.keys]
                    return self.generic_visit(node)
                def visit_Name(self, node):
                    if node.id in ('true','false','null'):
                        return ast.copy_location(ast.Constant({'true':True,'false':False,'null':None}[node.id]), node)
                    return node
            yield ast.literal_eval(Constants().visit(tree))
        except (ValueError, SyntaxError):
            continue


def arc_digest(value):
    # ArcMD5 hashes the low byte of each JavaScript UTF-16 code unit.
    low_bytes = value.encode('utf-16-le', errors='surrogatepass')[::2]
    return hashlib.sha512(hashlib.md5(low_bytes, usedforsecurity=False).hexdigest().encode()).hexdigest()

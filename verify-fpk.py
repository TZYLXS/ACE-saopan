# -*- coding: utf-8 -*-
"""校验 fpk：结构、manifest、checksum、脚本权限、python 语法。"""
import hashlib
import io
import os
import tarfile

FPK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "扫盘.fpk")

outer = tarfile.open(FPK, "r:*")
print("=== 外层 ===")
for m in outer.getmembers():
    print(("d" if m.isdir() else "-"), "%-24s" % m.name, "%7d" % m.size, "mode=%o" % m.mode)

manifest = outer.extractfile("manifest").read().decode("utf-8")
print("\n=== manifest ===")
print(manifest)

claim = [l.split("=")[1].strip() for l in manifest.splitlines() if l.startswith("checksum")]
app_tgz_bytes = outer.extractfile("app.tgz").read()
real = hashlib.md5(app_tgz_bytes).hexdigest()
print("checksum 声明=%s 实测=%s %s" % (claim[0], real, "OK" if claim[0] == real else "MISMATCH"))

inner = tarfile.open(fileobj=io.BytesIO(app_tgz_bytes), mode="r:*")
print("\n=== app.tgz ===")
for m in inner.getmembers():
    print(("d" if m.isdir() else "-"), "%-40s" % m.name, "%9d" % m.size, "mode=%o" % m.mode)

print("\n=== 内层 python 语法检查 ===")
import ast
for m in inner.getmembers():
    if m.name.endswith(".py"):
        code = inner.extractfile(m).read().decode("utf-8")
        try:
            ast.parse(code)
            print("OK   " + m.name)
        except SyntaxError as ex:
            print("FAIL " + m.name + " " + str(ex))

print("\n=== 内层 bash 语法（仅检查首行 shebang 与关键片段） ===")
for name in ("cmd/main", "cmd/install_init", "cmd/install_callback", "cmd/uninstall", "cmd/start_stop_restart"):
    code = outer.extractfile(name).read().decode("utf-8")
    print("\n---- %s ----\n%s" % (name, code))
outer.close()

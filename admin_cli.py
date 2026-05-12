#!/usr/bin/env python3
"""管理员 CLI — 用户管理 (开放注册已关闭, 账号由管理员分发)

用法:
  uv run admin_cli.py create <email> <nickname>   # 创建用户 (提示输入密码)
  uv run admin_cli.py list                         # 列所有用户
  uv run admin_cli.py delete <email>               # 删除用户 (含其所有数据)
  uv run admin_cli.py passwd <email>               # 重置密码并撤销 sessions
"""
import sys
import getpass
import datetime as dt

import db


def cmd_create(args):
    if len(args) < 2:
        print("用法: create <email> <nickname>"); return 1
    email = args[0].strip()
    nickname = " ".join(args[1:]).strip()
    if db.find_user_by_email(email):
        print(f"✗ 邮箱 {email} 已存在"); return 1
    pwd = getpass.getpass("密码 (≥6 位): ")
    if len(pwd) < 6:
        print("✗ 密码至少 6 位"); return 1
    pwd2 = getpass.getpass("再输一次: ")
    if pwd != pwd2:
        print("✗ 两次密码不一致"); return 1
    user = db.create_user(email, pwd, nickname)
    if user:
        print(f"✓ 创建成功: id={user['id']} email={user['email']} nickname={user['nickname']}")
        return 0
    print("✗ 创建失败 (db 错误)"); return 1


def cmd_list(args):
    with db.conn() as c:
        rows = c.execute("SELECT id, email, nickname, created_at FROM users ORDER BY id").fetchall()
    if not rows:
        print("(无用户)"); return 0
    print(f"{'ID':>4}  {'Email':<30}  {'Nickname':<20}  Created")
    print("-" * 78)
    for r in rows:
        ts = dt.datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
        print(f"{r['id']:>4}  {r['email']:<30}  {r['nickname']:<20}  {ts}")
    return 0


def cmd_delete(args):
    if not args:
        print("用法: delete <email>"); return 1
    user = db.find_user_by_email(args[0])
    if not user:
        print(f"✗ 没找到 {args[0]}"); return 1
    confirm = input(f"确认删除 {user['email']} (id={user['id']}, 含其所有 recordings/rooms)? [y/N]: ").strip().lower()
    if confirm != "y":
        print("已取消"); return 0
    with db.conn() as c:
        c.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    print(f"✓ 已删除 {args[0]}")
    return 0


def cmd_passwd(args):
    if not args:
        print("用法: passwd <email>"); return 1
    user = db.find_user_by_email(args[0])
    if not user:
        print(f"✗ 没找到 {args[0]}"); return 1
    pwd = getpass.getpass(f"新密码 ({args[0]}, ≥6 位): ")
    if len(pwd) < 6:
        print("✗ 密码至少 6 位"); return 1
    pwd2 = getpass.getpass("再输一次: ")
    if pwd != pwd2:
        print("✗ 两次密码不一致"); return 1
    with db.conn() as c:
        c.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                  (db.hash_password(pwd), user["id"]))
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    print("✓ 密码已更新, 所有登录 session 已撤销")
    return 0


COMMANDS = {"create": cmd_create, "list": cmd_list, "delete": cmd_delete, "passwd": cmd_passwd}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__); return 0
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"未知命令: {cmd}"); print(__doc__); return 1
    return COMMANDS[cmd](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main() or 0)

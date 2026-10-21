在当前工作区完成日志分析并生成实际产物：
1. 用 search_files 搜索 *.log 中的 ERROR，再用 read_file 完整读取 app.log；如果有 next_offset，继续分页直到 eof=true。
2. 使用 write_file 编写 analyze_logs.py，只使用 Python 标准库。脚本从当前工作目录读取 app.log，只统计级别为 ERROR 的行，按 code= 后的错误类型计数，排除 WARN 和 INFO。
3. 使用 run_command 执行 ["python", "analyze_logs.py"]，确保命令正常退出并生成 UTF-8 的 report.json。脚本必须实际计算数据，不能把统计结果硬编码。
4. report.json 的结构必须是 {"counts": {"错误类型": 次数}, "total_errors": ERROR总数}。
5. 使用 read_file 读取 report.json，核对字段与脚本执行结果后给出中文总结，说明产物位置。若执行失败，修复脚本并重试；不要声称已经通过独立验收。

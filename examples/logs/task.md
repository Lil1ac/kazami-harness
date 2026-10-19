读取工作区中的 app.log，只统计 ERROR 行，按 code 字段分组，给出每类次数和 ERROR 总数。不要把 WARN 和 INFO 算入。若工具返回 eof=false，使用 next_offset 继续读取，读完后再用中文输出统计表。

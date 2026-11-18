"""通过真实的 Django Admin 变更页读取可编辑与只读 JSON 字段；该验收探针仅供宿主。"""

import json
from html.parser import HTMLParser

from django.conf import settings

settings.configure(
    SECRET_KEY="isolated-reproduction", ROOT_URLCONF=__name__, ALLOWED_HOSTS=["testserver"],
    INSTALLED_APPS=["django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
                    "django.contrib.sessions", "django.contrib.messages"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    MIDDLEWARE=["django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
                "django.contrib.messages.middleware.MessageMiddleware"],
    TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True,
                "OPTIONS": {"context_processors": ["django.contrib.auth.context_processors.auth",
                                                    "django.contrib.messages.context_processors.messages"]}}],
)
import django
django.setup()
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, models
from django.test import Client
from django.urls import path, reverse


class Document(models.Model):
    """后台编辑的实际数据库对象，两个字段保存相同的中文内容。"""

    editable_json = models.JSONField()  # 普通表单控件显示。
    readonly_json = models.JSONField()  # ModelAdmin 把此字段设为只读。

    class Meta:
        """复用已安装应用，动态建表只影响本次内存数据库。"""

        app_label = "auth"  # 用于生成真实后台 URL 与模型元信息。


class DocumentAdmin(admin.ModelAdmin):
    """让同一变更页同时经过表单和后台只读字段的显示路径。"""

    readonly_fields = ("readonly_json",)  # 强制走后台展示逻辑。


class Values(HTMLParser):
    """从返回的 HTML 中提取目标 textarea 和只读展示元素的可见文字。"""

    def __init__(self):
        """初始化空的字段文本与当前捕获的结束标签。"""
        super().__init__()
        self.values = {}  # 每个页面控件对应实际可见文本。
        self.field = None  # 当前捕获字段；None 表示在目标元素之外。
        self.end = None  # 当前捕获元素的结束标签。

    def handle_starttag(self, tag, attrs):
        """按 tag 和 attrs 定位真实后台控件，返回空值。"""
        attrs = dict(attrs)
        if tag == "textarea" and attrs.get("name") == "editable_json":
            self.field = "editable"
        elif tag == "div" and "readonly" in attrs.get("class", "").split():
            self.field = "readonly"
        else:
            return
        self.end, self.values[self.field] = tag, ""

    def handle_data(self, data):
        """把目标元素内实际可见的 data 追加到字段。"""
        if self.field:
            self.values[self.field] += data

    def handle_endtag(self, tag):
        """遇到目标 tag 结束时停止捕获文字。"""
        if tag == self.end:
            self.field = self.end = None


admin.site.register(Document, DocumentAdmin)
urlpatterns = [path("admin/", admin.site.urls)]  # 走路由、权限、ModelAdmin 和模板的完整入口。
call_command("migrate", verbosity=0)
with connection.schema_editor() as editor:
    editor.create_model(Document)
user = get_user_model().objects.create_superuser("reviewer", "reviewer@example.invalid", "local-only")
value = {"city": "中国"}
row = Document.objects.create(editable_json=value, readonly_json=value)
client = Client()
client.force_login(user)
url = reverse("admin:auth_document_change", args=[row.pk])
response = client.get(url)
assert response.status_code == 200, (url, response.status_code)
reader = Values()
reader.feed(response.content.decode(response.charset))
assert set(reader.values) == {"editable", "readonly"}, reader.values
expected = json.dumps(value, ensure_ascii=False)
print("GET", url, response.status_code)
observations = {name: {"expected": expected, "actual": actual.strip()} for name, actual in reader.values.items()}
print("HARNESS_OBSERVATIONS", json.dumps(observations, ensure_ascii=False))
raise SystemExit(int(any(item["actual"] != expected for item in observations.values())))

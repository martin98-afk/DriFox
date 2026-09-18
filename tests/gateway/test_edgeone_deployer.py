# -*- coding: utf-8 -*-
"""
EdgeOneDeployer 单元测试（全程 mock，不触网）

覆盖：
  - 单例模式与便捷函数
  - zip 打包正确性（index.html 存在、UTF-8 内容一致）
  - 项目名生成（非法字符替换、随机后缀、空标题兜底）
  - COS 签名串结构（q-sign-algorithm / header-list / 临时 token 参与签名）
  - 全链路成功（五步按序调用并返回站点链接）
  - 各失败路径：临时凭证非 0 code、凭证缺字段、COS 非 200、
    创建部署 Response.Error、部署状态 Failed、轮询超时、空 HTML

Run: pytest tests/gateway/test_edgeone_deployer.py -v
"""

import io
import zipfile
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("app.gateway.utils.edgeone_deployer")

from app.gateway.utils.edgeone_deployer import (  # noqa: E402
    EdgeOneDeployer,
    EdgeOneDeployError,
    deploy_html_to_edgeone,
    get_edgeone_deployer,
)


# =============================================================================
# Fixtures / 工具
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个测试前重置单例，避免跨用例污染"""
    old = EdgeOneDeployer._instance
    EdgeOneDeployer._instance = None
    yield
    EdgeOneDeployer._instance = old


@pytest.fixture
def deployer():
    return EdgeOneDeployer.get_instance()


def _resp(status_code=200, json_body=None, text=""):
    """构造一个 requests 风格的假响应"""
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    if json_body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = json_body
    return r


def _temp_token_body(project_name="drifox-share-abcd1234"):
    """构造 describe_temp_token 的成功响应体"""
    return {
        "code": 0,
        "data": {
            "Token": "anon-token-abc",
            "Expired": 1789702689,
            "Response": {
                "ProjectName": project_name,
                "TargetPath": "abc123/proj/1789698772679",
                "Bucket": "eop-trans-sg-prod-1256816668",
                "Region": "ap-singapore",
                "ExpiredTime": 1789700890,
                "Expiration": "2026-09-18T03:08:10Z",
                "Credentials": {
                    "TmpSecretId": "AKIDtmp",
                    "TmpSecretKey": "SKtmp",
                    "Token": "cos-session-token-xyz",
                },
            },
        },
    }


def _create_deployment_body():
    return {
        "code": 0,
        "data": {"Response": {"ProjectId": "makers-ihirny", "DeploymentId": "dp6s6bq1"}},
    }


def _describe_body(status, url=""):
    dep = {"Status": status}
    if url:
        dep["ProjectUrl"] = url
    if status == "Failed":
        dep["Message"] = "构建脚本执行失败"
        dep["Code"] = -1
    return {"code": 0, "data": {"Response": {"Deployment": dep}}}


# =============================================================================
# 基础
# =============================================================================


def test_singleton_and_convenience():
    a = EdgeOneDeployer.get_instance()
    b = EdgeOneDeployer.get_instance()
    assert a is b
    assert get_edgeone_deployer() is a


def test_empty_html_rejected(deployer):
    url, err = deployer.deploy_html("")
    assert url is None
    assert "为空" in err

    url, err = deployer.deploy_html("   \n  ")
    assert url is None
    assert "为空" in err


def test_pack_single_page_zip_structure(deployer):
    html = "<html><body>中文内容 test</body></html>"
    raw = deployer._pack_single_page(html)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        assert zf.namelist() == ["index.html"]
        assert zf.read("index.html").decode("utf-8") == html


def test_make_project_name_sanitizes_title(deployer):
    name = deployer._make_project_name("对话分享 — EdgeOne 测试!")
    # 中文与非法字符被替换为中划线后折叠，仅保留合法片段
    assert "edgeone" in name
    # 只含小写字母、数字、中划线
    assert all(c.islower() or c.isdigit() or c == "-" for c in name)
    assert not name.startswith("-") and not name.endswith("-")
    assert len(name) <= 34
    # 两次生成后缀不同（随机后缀防撞名）
    assert deployer._make_project_name("x") != deployer._make_project_name("x")


def test_make_project_name_fallback_for_empty_title(deployer):
    assert deployer._make_project_name("").startswith("drifox-share-")
    assert deployer._make_project_name("中文标题").startswith("drifox-share-")


def test_cos_authorization_structure(deployer):
    cred = {"TmpSecretId": "AKIDtmp", "TmpSecretKey": "SKtmp", "Token": "session-token"}
    auth = deployer._build_cos_authorization(cred, "bucket.cos.ap-singapore.myqcloud.com", "prefix/site.zip")
    assert auth.startswith("q-sign-algorithm=sha1")
    assert "q-ak=AKIDtmp" in auth
    assert "q-sign-time=" in auth and "q-key-time=" in auth
    # 临时密钥必须把 x-cos-security-token 纳入签名单
    assert "x-cos-security-token" in auth
    assert "host" in auth
    assert "q-signature=" in auth


# =============================================================================
# 全链路
# =============================================================================


def test_deploy_html_full_success(deployer):
    site = "https://drifox-share-abcd1234.edgeone.dev?eo_token=t&eo_time=123"
    with patch.object(deployer, "_session") as session:
        session.post.side_effect = [
            _resp(200, _temp_token_body()),
            _resp(200, _create_deployment_body()),
            _resp(200, _describe_body("Process")),
            _resp(200, _describe_body("Success", site)),
        ]
        session.put.return_value = _resp(200)
        with patch("app.gateway.utils.edgeone_deployer.time.sleep") as mock_sleep:
            url, err = deployer.deploy_html("<html>hi</html>", "测试标题")

    assert err is None
    assert url == site
    # 五步：temp_token → create_deployment → describe×2
    assert session.post.call_count == 4
    assert session.put.call_count == 1
    # COS 上传落在 TargetPath 下
    put_url = session.put.call_args[0][0]
    assert "abc123/proj/1789698772679/site.zip" in put_url
    assert "eop-trans-sg-prod-1256816668.cos.ap-singapore.myqcloud.com" in put_url
    # 描述轮询在 Process 后等待了一次
    assert mock_sleep.call_count >= 1


def test_deploy_html_convenience_function():
    with patch.object(EdgeOneDeployer, "deploy_html", return_value=("https://x.edgeone.dev", None)) as m:
        url, err = deploy_html_to_edgeone("<html></html>", "t")
    assert url == "https://x.edgeone.dev"
    assert err is None
    m.assert_called_once_with("<html></html>", "t")


# =============================================================================
# 失败路径
# =============================================================================


def test_temp_token_business_error(deployer):
    with patch.object(deployer, "_session") as session:
        session.post.return_value = _resp(200, {"code": 40001, "message": "频率超限"})
        url, err = deployer.deploy_html("<html></html>")
    assert url is None
    assert "申请临时凭证失败" in err
    assert "频率超限" in err


def test_temp_token_http_error(deployer):
    with patch.object(deployer, "_session") as session:
        session.post.return_value = _resp(502, None, "Bad Gateway")
        url, err = deployer.deploy_html("<html></html>")
    assert url is None
    assert "HTTP 502" in err


def test_temp_token_missing_fields(deployer):
    body = _temp_token_body()
    body["data"]["Response"].pop("Bucket")
    with patch.object(deployer, "_session") as session:
        session.post.return_value = _resp(200, body)
        url, err = deployer.deploy_html("<html></html>")
    assert url is None
    assert "缺少必要字段" in err


def test_cos_upload_failure(deployer):
    with patch.object(deployer, "_session") as session:
        session.post.return_value = _resp(200, _temp_token_body())
        session.put.return_value = _resp(403, None, "SignatureDoesNotMatch")
        url, err = deployer.deploy_html("<html></html>")
    assert url is None
    assert "上传产物失败" in err
    assert "403" in err


def test_create_deployment_response_error(deployer):
    body = {"code": 0, "data": {"Response": {"Error": {"Code": "LimitExceeded", "Message": "项目数超限"}}}}
    with patch.object(deployer, "_session") as session:
        session.post.side_effect = [_resp(200, _temp_token_body()), _resp(200, body)]
        session.put.return_value = _resp(200)
        url, err = deployer.deploy_html("<html></html>")
    assert url is None
    assert "创建部署失败" in err
    assert "项目数超限" in err


def test_create_deployment_business_error(deployer):
    with patch.object(deployer, "_session") as session:
        session.post.side_effect = [
            _resp(200, _temp_token_body()),
            _resp(200, {"code": 500, "message": "内部错误"}),
        ]
        session.put.return_value = _resp(200)
        url, err = deployer.deploy_html("<html></html>")
    assert url is None
    assert "创建部署失败" in err
    assert "内部错误" in err


def test_deployment_status_failed(deployer):
    with patch.object(deployer, "_session") as session:
        session.post.side_effect = [
            _resp(200, _temp_token_body()),
            _resp(200, _create_deployment_body()),
            _resp(200, _describe_body("Failed")),
        ]
        session.put.return_value = _resp(200)
        url, err = deployer.deploy_html("<html></html>")
    assert url is None
    assert "部署失败" in err
    assert "构建脚本执行失败" in err


def test_deployment_poll_timeout(deployer):
    with patch.object(deployer, "_session") as session:
        session.post.side_effect = [_resp(200, _temp_token_body()), _resp(200, _create_deployment_body())]
        session.put.return_value = _resp(200)
        # 把超时窗口设为负值，第一轮即到期，避免真等 90 秒
        with patch.object(deployer, "POLL_TIMEOUT_S", -1), patch("app.gateway.utils.edgeone_deployer.time.sleep"):
            url, err = deployer.deploy_html("<html></html>")
    assert url is None
    assert "部署超时" in err


def test_deployment_success_without_url(deployer):
    with patch.object(deployer, "_session") as session:
        session.post.side_effect = [
            _resp(200, _temp_token_body()),
            _resp(200, _create_deployment_body()),
            _resp(200, _describe_body("Success")),
        ]
        session.put.return_value = _resp(200)
        url, err = deployer.deploy_html("<html></html>")
    assert url is None
    assert "未返回站点链接" in err


def test_connection_error_mapped_to_readable_message(deployer):
    import requests

    with patch.object(deployer, "_session") as session:
        session.post.side_effect = requests.exceptions.ConnectionError("boom")
        url, err = deployer.deploy_html("<html></html>")
    assert url is None
    assert "网络连接失败" in err


def test_deploy_error_class_is_exception():
    err = EdgeOneDeployError("x")
    assert isinstance(err, Exception)
    assert str(err) == "x"

"""tests for looks_like_bot_challenge — 正例/反例（含 captcha 资源名不误报）。"""

import pytest

from src.anti_detection.curl_client import looks_like_bot_challenge


# ---------------------------------------------------------------------------
# 反例：正常页面（必须判 False）
# ---------------------------------------------------------------------------

NORMAL_PRODUCT_PAGE = """
<!DOCTYPE html><html><head>
<title>Amazon Basics Cotton Towels, 6-Pack : Home & Kitchen</title>
<script src="https://images-na.ssl-images-amazon.com/images/I/91vvWideHost.js"></script>
</head><body class="a-m-us">
<div id="productTitle">Amazon Basics Cotton Towels, 6-Pack</div>
<span class="a-price"><span class="a-offscreen">$18.99</span></span>
<script>
// 前端资源清单里常见 captcha 字样——不能因资源文件名误报
var CaptchaJS = "/aapi/captcha-render/captcha.js";
var imgUrl = "https://m.media-amazon.com/images/G/01/x-locale/common/captcha-icon._CB_.png";
</script>
<div id="availability"><span>In Stock</span></div>
</body></html>
"""

NORMAL_SEARCH_PAGE = """
<html><head><title>Amazon.com : towels</title></head>
<body>
<a href="/s?k=towels">results</a>
<!-- search metrics script mentions captcha-solution token generator -->
<script>var captchaSolutionEndpoint = "/aapi/captcha/solution";</script>
</body></html>
"""

EMPTY_TEXT = ""


# ---------------------------------------------------------------------------
# 正例：挑战/拦截页（必须判 True）
# ---------------------------------------------------------------------------

ROBOT_CHECK_PAGE = """
<html><head><title>Robot Check</title></head>
<body>
<h4>Enter the characters you see below</h4>
<div class="a-box"><img src="https://images-na.ssl-images-amazon.com/captcha/usxffffff/Captcha_image.jpg"></div>
</body></html>
"""

SORRY_503_PAGE = """
<html><head><title>Sorry! Something went wrong!</title></head>
<body>
<p>Your request could not be processed. For assistance, contact
api-services-support@amazon.com</p>
</body></html>
"""

ACCESS_DENIED_PAGE = """
<html><head><title>Access Denied</title></head>
<body><p>You don't have permission to access this resource.</p></body></html>
"""

TITLE_CAPTCHA_PAGE = """
<html><head><title>Please complete the captcha challenge</title></head>
<body></body></html>
"""

CAPTCHA_IMAGE_ELEMENT_PAGE = """
<html><head><title>Enter the characters you see below</title></head>
<body><img id="captcha-image" src="/captcha/us/Captcha_image.jpg"></body></html>
"""

CAPTCHA_IMAGE_CLASS_PAGE = """
<html><body><div class="captcha-image-container"><img src="/captcha/x.jpg"></div>
<script src="https://bFX.awsWafToken.js"></script>
</body></html>
"""

AWS_WAF_TOKEN_SCRIPT_PAGE = """
<html><head><title>Amazon.com</title></head>
<body><script src="https://cf.aws-waf-token-challenge.js"></script></body></html>
"""


class TestNegativeCases:
    """正常页面不能误判为挑战页。"""

    def test_normal_product_page_false(self):
        assert looks_like_bot_challenge(NORMAL_PRODUCT_PAGE) is False

    def test_normal_search_page_false(self):
        assert looks_like_bot_challenge(NORMAL_SEARCH_PAGE) is False

    def test_empty_text_false(self):
        assert looks_like_bot_challenge(EMPTY_TEXT) is False

    def test_captcha_resource_name_does_not_trigger(self):
        """正文含 captcha 资源名（JS URL / 图片路径 / 变量名）不误报。"""
        page = (
            '<html><head><title>Amazon.com: PS5</title></head><body>'
            '<script src="/aapi/captcha-render/captcha.js"></script>'
            '<img src="https://m.media-amazon.com/captcha-icon.png">'
            '</body></html>'
        )
        assert looks_like_bot_challenge(page) is False

    def test_plain_word_captcha_in_body_does_not_trigger(self):
        """正文裸词 captcha（非 title / 非元素 id/class）不算强特征。"""
        page = (
            '<html><head><title>Help - Amazon Customer Service</title></head>'
            '<body><p>Sometimes you may be asked to solve a captcha.</p></body></html>'
        )
        assert looks_like_bot_challenge(page) is False


class TestPositiveCases:
    """挑战/拦截页必须命中。"""

    @pytest.mark.parametrize(
        'html', [ROBOT_CHECK_PAGE, SORRY_503_PAGE, ACCESS_DENIED_PAGE,
                 TITLE_CAPTCHA_PAGE]
    )
    def test_title_markers_true(self, html):
        assert looks_like_bot_challenge(html) is True

    def test_sorry_email_marker_true(self):
        """api-services-support@amazon.com 邮箱指纹独立命中（即使 title 正常）。"""
        page = (
            '<html><head><title>Page</title></head>'
            '<body><p>contact api-services-support@amazon.com</p></body></html>'
        )
        assert looks_like_bot_challenge(page) is True

    def test_captcha_image_id_true(self):
        assert looks_like_bot_challenge(CAPTCHA_IMAGE_ELEMENT_PAGE) is True

    def test_captcha_image_class_true(self):
        assert looks_like_bot_challenge(CAPTCHA_IMAGE_CLASS_PAGE) is True

    def test_aws_waf_token_script_true(self):
        assert looks_like_bot_challenge(AWS_WAF_TOKEN_SCRIPT_PAGE) is True

//! 自带的 Web 界面。
//!
//! 页面在编译期嵌进二进制（`include_str!`），所以 `static/` 不进镜像、
//! `Dockerfile` 与 `.dockerignore` 都不用改，文件缺失也会在编译期直接报错。
//! 这也是不引入 `ServeDir` 这类依赖的原因：三个文件不值得再加一层中间件。
//!
//! 设计依据、交互约定与安全取舍见 `docs/ocr-ui.md`。
//!
//! 页面挂在**根路径** `/`：那是人（和手机）唯一会手输的地址，点开域名就该看到它。
//! 样式与脚本在 `/app.css`、`/app.js`。

use axum::body::Body;
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};

const INDEX_HTML: &str = include_str!("../static/index.html");
const APP_CSS: &str = include_str!("../static/app.css");
const APP_JS: &str = include_str!("../static/app.js");

/// 页面的内容安全策略。
///
/// `default-src 'none'` 起手，只放开同源资源：页面不引用任何 CDN 或第三方脚本
/// （运行环境不保证外网可达），`style-src 'self'` 又顺带保证了
/// "位置框坐标只能走 CSSOM" 这条实现约束不被绕开。
const CONTENT_SECURITY_POLICY: &str = "default-src 'none'; \
     img-src 'self' blob: data:; \
     connect-src 'self'; \
     style-src 'self'; \
     script-src 'self'; \
     base-uri 'none'; \
     form-action 'none'";

/// 页面与静态资源都免鉴权：容器或手机得先能把页面打开，才谈得上填 token。
/// 三条路由只暴露页面结构，不含任何敏感数据。
pub async fn index() -> Response {
    asset(INDEX_HTML, "text/html; charset=utf-8")
}

pub async fn css() -> Response {
    asset(APP_CSS, "text/css; charset=utf-8")
}

pub async fn js() -> Response {
    asset(APP_JS, "application/javascript; charset=utf-8")
}

fn asset(body: &'static str, content_type: &'static str) -> Response {
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, content_type),
            // 换镜像后手机上刷到旧页面，比多几百字节的请求麻烦得多
            (header::CACHE_CONTROL, "no-cache"),
            (header::CONTENT_SECURITY_POLICY, CONTENT_SECURITY_POLICY),
        ],
        Body::from(body),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn assets_are_embedded_and_look_like_themselves() {
        assert!(INDEX_HTML.contains("<title>"));
        assert!(INDEX_HTML.contains("/app.js"));
        assert!(!INDEX_HTML.contains("/ui/"), "页面还引用着旧的 /ui 前缀");
        assert!(APP_CSS.contains("--accent"));
        assert!(APP_JS.contains("X-Auth-Token"));
    }

    #[test]
    fn policy_blocks_everything_but_same_origin() {
        assert!(CONTENT_SECURITY_POLICY.contains("default-src 'none'"));
        assert!(CONTENT_SECURITY_POLICY.contains("script-src 'self'"));
        assert!(CONTENT_SECURITY_POLICY.contains("connect-src 'self'"));
    }
}

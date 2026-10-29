# 第三方资源许可说明

本仓库是**混合授权**：原创源代码用 MIT，动画媒体资源受上游单独条款约束。
两者不能混为一谈。

## 适用范围

- 仓库根目录 [LICENSE](LICENSE) 的 MIT 授权**仅覆盖原创源代码**
  （`src/`、`tests/`、`main.py`、`dsh-plugin/` 等）
- 该 MIT 授权**不适用于**动画帧素材（本仓库不包含该素材，见下）

## 动画资源

- **来源**：[PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)
- **上游资源路径**：`assets/thumb/`
- **上游作者**：PC2005-cloud/dsh-pet contributors

本项目使用的动画帧由上游动画素材转制而来（webm → WebP 帧序列），
仅改变了封装格式与尺寸，动作本身由上游创作。

### 上游条款

上游 README 对动画、提示词及源视频等资源声明：**允许在开源项目中使用，但禁止商业使用**。

因此这些动画资源**不是**由本项目以 MIT License 重新授权；复制、分发、修改或使用这些
资源时，必须同时遵守上游的资源条款并**保留来源署名**。

上游条款可能变化，使用前请核对：

- [上游 README（资源许可说明）](https://github.com/PC2005-cloud/dsh-pet#license)
- [上游 LICENSE](https://github.com/PC2005-cloud/dsh-pet/blob/main/LICENSE)

如需商业使用，请在使用前联系上游权利人取得明确授权。若不接受上述媒体资源条款，
请勿复制、分发或使用这些动画文件。

## 本仓库为何不含素材

1. 帧素材 834 MB（`frames/`，106 个动作 / 25 400 张 WebP），不适合放进 git；
2. 素材受上述禁商用条款约束，不作为 MIT 代码的一部分分发。

需要运行请自行从上游获取动画资源，并自行完成转帧与 `config.jsonc` 配置。

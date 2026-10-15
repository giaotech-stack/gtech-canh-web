# gtech-canh-web

Công cụ nhỏ tự dựng để **canh sức khỏe các ứng dụng đang chạy thật** và báo qua Telegram. Không dùng dịch vụ trả phí: chạy bằng GitHub Actions (repo công khai thì miễn phí, không giới hạn phút).

## Mục đích

Cứ 15 phút, GitHub Actions gọi endpoint sức khỏe (ví dụ `/api/health`) của từng ứng dụng. Ứng dụng được coi là **UP** khi trả HTTP 200 và JSON có `"status":"ok"`; ngược lại là **DOWN** (ví dụ 503 khi mất kết nối CSDL, hết thời gian chờ, JSON hỏng).

## Cách hoạt động

- Mỗi lần chạy, mỗi target được gọi tối đa 3 lần (timeout 20 giây, cách nhau 20 giây) trước khi kết luận DOWN, để tránh báo nhầm do mạng chớp. Tổng thời gian cho một target bị chặn khoảng 75 giây (đồng hồ monotonic): không đủ thời gian thì bỏ bớt lần thử, và lần thử cuối có timeout rút ngắn. Lưu ý: phân giải DNS đôi khi vẫn có thể chặn lâu hơn timeout của socket (hiếm); vì vậy job có thêm giới hạn cứng `timeout-minutes: 12`.
- Chỉ báo khi **đổi trạng thái**:
  - UP sang DOWN: báo 🔴 ngay, kèm mã lý do (`HTTP 503`, `timeout`...) và giờ Việt Nam.
  - Vẫn DOWN: nhắc lại mỗi 60 phút (cho phép lệch lịch tối đa 5 phút).
  - DOWN sang UP: báo 🟢 kèm thời gian đã ngừng.
  - Lần chạy đầu tiên (chưa có state): chỉ báo nếu đang DOWN.
- State (đang UP hay DOWN, từ lúc nào, lần báo cuối, đã báo được chưa) lưu giữa các lượt bằng `actions/cache`, khóa theo **số thứ tự dòng** trong `TARGETS`. Đừng đổi thứ tự các dòng khi đang có target DOWN, nếu không state sẽ bị gán nhầm. State được ghi (nguyên tử) ngay sau mỗi target, nên job bị hủy giữa chừng cũng không báo lặp. State mất, hết hạn hoặc hỏng (từng mục) thì mục đó coi như lần đầu: DOWN vẫn báo, UP thì im lặng.
- Gửi Telegram thất bại thì chương trình thoát mã 1 để lượt chạy báo đỏ (GitHub gửi mail cho chủ repo). Lần phát hiện DOWN đầu tiên vẫn được ghi (giữ giờ phát hiện) kèm cờ `alerted: false` nên lượt sau báo lại; tin nhắc/tin hồi phục thất bại thì giữ nguyên state cũ để báo lại. Target DOWN tự nó **không** làm lượt chạy đỏ; tin Telegram mới là tín hiệu.
- Chỉ dùng thư viện chuẩn của Python 3, mã nguồn là `canh.py`.

## 3 secret cần đặt

Vào Settings, Secrets and variables, Actions, New repository secret:

| Tên | Nội dung |
|---|---|
| `TARGETS` | Nhiều dòng, mỗi dòng `nhãn\|https://.../api/health`. Dòng trống được bỏ qua. |
| `TELEGRAM_BOT_TOKEN` | Token bot do BotFather cấp. |
| `TELEGRAM_CHAT_ID` | ID chat (cá nhân hoặc nhóm) nhận tin. Bot phải đã được nhắn tin hoặc thêm vào chat đó. |

Thiếu bất kỳ secret nào, hoặc `TARGETS` sai dạng, lượt chạy sẽ **đỏ** kèm thông báo rõ (không im lặng bỏ qua). Thông báo lỗi chỉ nêu số dòng, không lặp lại nội dung.

## Chạy thử báo động

Actions, chọn workflow `canh-web`, Run workflow, tick `thu_bao_dong`, Run. Chương trình tạo thêm một target giả từ URL của dòng đầu tiên (thêm `-thu-loi` vào cuối đường dẫn, chắc chắn 404), nhãn "(THỬ báo động)", luôn gửi tin 🔴, rồi gửi tin "kết thúc thử". State của target giả không được lưu. Nhận đủ 2 tin Telegram là đường báo động thông.

## Giới hạn đã biết

- **Lịch GitHub có thể trễ 5 đến 15 phút**, đôi khi bỏ lượt lúc tải cao. Đây không phải giám sát thời gian thực; sự cố ngắn hơn chu kỳ có thể lọt.
- **Tự tắt sau 60 ngày**: GitHub vô hiệu hóa workflow theo lịch ở repo công khai nếu repo không có hoạt động trong 60 ngày. Workflow có bước "Giữ lịch chạy" (tối đa 1 lần/ngày) gọi API `enable` bằng `GITHUB_TOKEN`. **Chưa chắc** việc này có tính là "hoạt động" để reset bộ đếm; GitHub không cam kết. Cách dự phòng: mỗi tháng một lần bấm tay "Enable workflow" ở tab Actions, hoặc đẩy một commit rỗng (`git commit --allow-empty -m "giữ lịch" && git push`). Nếu nhận mail "scheduled workflows disabled" thì làm ngay. Nếu muốn bỏ quyền `actions: write`, xóa bước này và đổi quyền về `contents: read`.
- Chỉ canh được thứ **trả lời HTTP công khai** từ internet. Không thấy được mạng nội bộ hoặc dịch vụ cần đăng nhập.
- Nếu chính GitHub Actions hoặc Telegram gián đoạn thì không có tin báo.
- Mỗi lượt lưu một cache nhỏ; cache cũ tự bị xóa sau 7 ngày không dùng.

## Nguyên tắc an toàn

Repo này **công khai**, log Actions cũng công khai.

- **Không ghi URL, tên khách hàng, token vào repo**, commit message, issue hay log. Mọi thứ đó nằm trong secret.
- Log chỉ in `target #N: UP/DOWN (mã)` và `alert sent: yes/no`; không in URL, nhãn hay token. Lưu ý: người ngoài vẫn thấy được trạng thái UP/DOWN ẩn danh của "target #N" qua log công khai.
- Tin nhắn Telegram được lọc lần cuối: che token bot, URL và mọi chuỗi từ 9 chữ số trở lên.
- Ví dụ trong test chỉ dùng `https://example.com`.
- Workflow `test.yml` (chạy khi push/PR) không dùng secret nào.

## Chạy test cục bộ

```bash
python3 -m unittest -v
```

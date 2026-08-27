# SafeZone Group — Cloud

A Cloud Adapter will be installed at the customer site. It connects to the cameras/recorder over the local network, pulls the video stream and uploads it to the cloud (possibly over HTTPS). The user watches the video through a website/application — the stream comes from the cloud rather than directly from the camera. Because of this, no port forwarding required on the customer's router.

**Components:**

1. Cloud Adapter
2. Cloud backend — reception, storage and processing of video
3. Level 1 web panel.
4. Level 2 web panel — for dealers.
5. Level 3 web panel — for end customers
4. Mobile application (iOS/Android) - separate project, - 2nd Phase.
5. AI analytics module (How realistic this is — we'll see how it turns out) – Under investigation, not planned yet
6. Billing and subscription management (- simplified for 1st Phase, 1st Project)

## 2. GATEWAY DEVICE / CLIENT AGENT

Functionality:

- Automatic discovery of cameras on the local network via the ONVIF protocol
- Connection to RTSP streams (H.264/H.265 codecs, depends on hardware)
- Local video buffering when the internet connection drops, with automatic upload of the backlog once the connection is restored
- Installation without port forwarding — an outbound connection to the cloud (AWS Kinesis Video Stream or persistent WebSocket/gRPC or VPN tunnel)
- OTA firmware/software updates (weekly, without user involvement) – moved to forthcoming Phase
- Support for operation over a 4G/5G modem/router – under investigation
- Embedded Linux (Yocto/Buildroot) or an off-the-shelf mini-PC running Linux - moved to forthcoming Phase.
- FFmpeg — stream transcoding (- only if required by other modules of the system, the choice of framework is open)
- MQTT or ~~gRPC~~ — communication with the cloud

**These abbreviations are not familiar to me. I came across them while researching and have added them here.**

## 3. CLOUD

Functionality:

- Ingest service for receiving video streams, scalable via message queues (~~Kafka/RabbitMQ~~) - AWS Kinesis Video Stream
- Storage: object storage (S3-compatible) for video segments + a relational database (PostgreSQL) for metadata (time, camera, object, events). - Moved to the 2nd Phase.
  1st Phase – Kinesis Video Stream – Hot tier or warm tier storage
- Transcoding for different quality levels (adaptive bitrate for mobile/desktop)
- Retention policy — automatic deletion of video once the retention period expires (7 / 30 / 90 days, ~~up to 10 years~~)
- Encryption: TLS in transit, ~~AES-256 at rest~~. - AWS provided encryption
- Multi-tenancy: data isolation per customer/site, role-based access (admin / dealer / customer)

## 4. AI ANALYTICS (Under investigation, optional)

A separate service for processing the video stream or the recorded segments:

- Detection of people / vehicles / animals (YOLO-type models, fine-tuned for CCTV camera angles)
- License plate recognition (LPR) — a separate model, requires high frame resolution
- Visitor counting (line crossing / zone-based counting)
- Smart Search — searching by object and time without reviewing the entire archive
- Real-time alerts (push / email / SMS)

Note: this is the most expensive and most time-consuming part of the system to develop. At the start it is more sensible to integrate ready-made APIs (AWS Rekognition, Azure Video Indexer) instead of developing proprietary models from scratch.

## 5. WEB PANEL

Functionality:

- Live view: grid mode, layouts, full-screen mode
- Archive with a scroll bar, clip export
- ~~PTZ camera control, two-way audio~~ - Support of PTZ camera moved to 2nd Phase. - One-way audio
- User and role management, ~~audit logs~~
- Switching between a customer's several ~~sites~~ locations
- Device status dashboard (online/offline, camera diagnostics)

React or Vue + an HLS/WebRTC video player for live view.

## 6. MOBILE APPLICATION (iOS / Android) (2nd Phase)

A stripped-down version of the web panel:

- Live camera view
- Push notifications
- Quick access to the archive

## 7. BILLING (1st Phase – simplified)

- Pricing based on the number of cameras, resolution and retention period
- Integration with a payment system
- Trial period
- Upgrade/downgrade of the tariff plan

## 8. WHERE TO START (1st Phase, 1st Project)

A realistic first stage:

1. Edge agent on a mini-PC (Raspberry Pi / Intel NUC), pulling RTSP from 1–4 cameras
2. Uploading video to S3 (- AWS storage: Hot tier/ warm tier) over HTTPS
3. A simple web player: live view + a 7-day archive
4. Basic motion detection (no AI — ordinary motion detection)
5. One tariff plan, payment handled manually

Once the MV~~P~~S (Minimum Viable Security) is working and can be shown to customers, AI analytics and automated billing ~~are~~ will be added on 2nd Phase.

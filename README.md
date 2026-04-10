
YourTreasurer 🏛️
Digital CFO for Students | CommitVerse 2026 Submission

YourTreasurer is a high-impact personal finance tool designed to act as a "financial bodyguard" for students. Beyond simple tracking, it manages monthly allowances, handles social liabilities (peer-to-peer loans), and ensures audit-ready compliance for campus life through automated logic and cloud integration.

✨ Key Features
Guardian Mail Logic: Automated Flask-Mail alerts triggered at 10%, 5%, and 0% budget thresholds.

30-Day Auto-Reset: Smart temporal logic that archives current data and resets the budget cycle every month.

Loan Handshake: Automated email notifications sent to friends immediately when a loan is logged to ensure transparency.

Receipt Vault: Secure Cloudinary integration for digital invoice storage and retrieval.

Visual Intelligence: Dynamic Chart.js dashboard featuring interactive Day vs. Month comparison toggles.

🛠️ Tech Stack
Backend: Python (Flask)

Database: MongoDB Atlas (NoSQL Cloud Hosting)

Storage: Cloudinary (Media/Receipt Management)

Communication: Flask-Mail (SMTP Integration)

Frontend: HTML5, CSS3 (Glassmorphism UI), JavaScript (AJAX for seamless updates)

🚀 Quick Start
1. Clone the Repository
Bash
git clone https://github.com/commitverse2026/YourTreasurer.git
cd YourTreasurer
2. Install Dependencies
Bash
pip install -r requirements.txt
3. Setup Environment Variables
Create a .env file in the root directory and configure the following:

MONGO_URI: Your MongoDB Atlas connection string.

MAIL_USERNAME & MAIL_PASSWORD: Your SMTP credentials.

CLOUDINARY_URL: Your Cloudinary API environment variable.

4. Run the Application
Bash
python app.py
📊 Implementation Roadmap
[x] Foundation: Budget Gateway & 30-Day Reset Logic

[x] Automation: Tiered Mail Alerts & Receipt Vaulting

[x] Intelligence: Aggregation API & Dynamic Data Toggles

[x] Liabilities: EMI Manager & Peer Debt Recovery Logic

[x] Polish: Real-time Progress Bars & UI Animations

Developed with ❤️ for CommitVerse 2026

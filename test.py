from dotenv import load_dotenv
load_dotenv()
import smtplib

EMAIL_ADDRESS = "nexusboard.project@gmail.com"
EMAIL_PASSWORD = "ewylzjdpxzrbanhq"


with smtplib.SMTP("smtp.gmail.com", 587) as server:
    server.starttls()
    server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
    print("✅ Gmail login successful")

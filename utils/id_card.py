from datetime import timedelta
import os
import io
import qrcode
import base64
from flask import current_app, url_for
from utils.image_generator import generate_image_from_html
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from models import StudentProfile


def _generate_fallback_pdf(student, file_path, profile_pic_path, logo_path,
                           index_number, programme_name, date_issue, date_expiry,
                           qr_base64):
    card_width, card_height = 85.6 * mm, 53.98 * mm
    pdf = canvas.Canvas(file_path, pagesize=(card_width, card_height))

    pdf.setFillColor(colors.HexColor('#12355b'))
    pdf.rect(0, 0, card_width, card_height, fill=1, stroke=0)
    if os.path.exists(logo_path):
        pdf.drawImage(logo_path, 5 * mm, card_height - 17 * mm, width=15 * mm,
                      height=12 * mm, preserveAspectRatio=True, mask='auto')
    if os.path.exists(profile_pic_path):
        pdf.drawImage(profile_pic_path, 7 * mm, 16 * mm, width=25 * mm,
                      height=25 * mm, preserveAspectRatio=True, mask='auto')

    pdf.setFillColor(colors.white)
    pdf.setFont('Helvetica-Bold', 9)
    pdf.drawString(24 * mm, card_height - 9 * mm, 'VTIU COLLEGE')
    pdf.setFont('Helvetica-Bold', 8)
    pdf.drawString(36 * mm, 34 * mm, str(getattr(student, 'full_name', 'Student'))[:30])
    pdf.setFont('Helvetica', 6.5)
    pdf.drawString(36 * mm, 29 * mm, f'INDEX: {index_number}')
    pdf.drawString(36 * mm, 25 * mm, str(programme_name)[:32])
    pdf.drawString(36 * mm, 21 * mm, f'ISSUED: {date_issue}  EXPIRES: {date_expiry}')
    pdf.setFont('Helvetica-Bold', 6)
    pdf.drawString(7 * mm, 9 * mm, 'STUDENT ID CARD')
    pdf.showPage()

    pdf.setFillColor(colors.white)
    pdf.rect(0, 0, card_width, card_height, fill=1, stroke=0)
    pdf.setFillColor(colors.HexColor('#12355b'))
    pdf.rect(0, card_height - 12 * mm, card_width, 12 * mm, fill=1, stroke=0)
    pdf.setFillColor(colors.black)
    pdf.setFont('Helvetica-Bold', 8)
    pdf.drawString(7 * mm, card_height - 8 * mm, 'VTIU COLLEGE - STUDENT CARD')
    pdf.setFont('Helvetica', 6.5)
    pdf.drawString(7 * mm, card_height - 20 * mm, 'This card remains the property of VTIU College.')
    pdf.drawString(7 * mm, card_height - 25 * mm, 'Return it to the College if found or when requested.')
    try:
        qr_image = ImageReader(io.BytesIO(base64.b64decode(qr_base64.split(',', 1)[1])))
        pdf.drawImage(qr_image, card_width - 27 * mm, 7 * mm, width=20 * mm,
                      height=20 * mm, preserveAspectRatio=True, mask='auto')
    except (ValueError, IndexError):
        pass
    pdf.save()

def generate_student_id_card_pdf(student):
    """
    Generate a CR80-sized student ID card as a PNG image.
    CR80 standard: 85.6mm x 53.98mm (3.370" x 2.125")
    
    FIXED: Handles profile picture paths correctly (no double paths)
    Returns the relative URL to the PNG file.
    """

    # Output directory
    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'id_cards')
    os.makedirs(upload_dir, exist_ok=True)

    # =====================================================
    # FUNCTION: Convert image to base64 for embedding in HTML
    # =====================================================
    def image_to_base64(image_path):
        """Convert an image file to base64 data URI"""
        if not image_path or not os.path.exists(image_path):
            return None
        
        try:
            with open(image_path, 'rb') as img_file:
                img_data = base64.b64encode(img_file.read()).decode('utf-8')
                # Determine MIME type
                ext = os.path.splitext(image_path)[1].lower()
                mime_type = {
                    '.png': 'image/png',
                    '.jpg': 'image/jpeg',
                    '.jpeg': 'image/jpeg',
                    '.gif': 'image/gif',
                }.get(ext, 'image/png')
                return f"data:{mime_type};base64,{img_data}"
        except Exception as e:
            print(f"Error converting image to base64: {e}")
            return None

    # =====================================================
    # Prepare images - FIX: Handle path correctly
    # =====================================================
    
    # Profile picture - FIXED: Extract filename if full path is stored
    profile_pic_filename = student.profile_picture or 'default_avatar.png'
    
    # Strip any path components (in case full path is stored)
    if profile_pic_filename and '/' in profile_pic_filename:
        profile_pic_filename = os.path.basename(profile_pic_filename)
    if profile_pic_filename and '\\' in profile_pic_filename:
        profile_pic_filename = os.path.basename(profile_pic_filename)
    
    # Now construct the correct path
    profile_pic_path = os.path.join(
        current_app.root_path, 
        'static', 
        'uploads', 
        'profile_pictures', 
        profile_pic_filename
    )
    
    # If file doesn't exist, use default
    if not os.path.exists(profile_pic_path):
        profile_pic_path = os.path.join(current_app.root_path, 'static', 'default_avatar.png')
    
    # If default doesn't exist either, create a blank one
    if not os.path.exists(profile_pic_path):
        print(f"Warning: No profile picture or default avatar found at {profile_pic_path}")
        # Use a transparent PNG as fallback
        profile_pic_base64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    else:
        profile_pic_base64 = image_to_base64(profile_pic_path)
        if not profile_pic_base64:
            # Fallback if conversion fails
            profile_pic_base64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    # School logo
    logo_path = os.path.join(current_app.root_path, 'static', 'VTIU-LOGO.png')
    logo_base64 = None
    if os.path.exists(logo_path):
        logo_base64 = image_to_base64(logo_path)
    
    # QR code
    qr_data = url_for('student.view_id_card', _external=True)
    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=8,
        border=1
    )
    qr.add_data(qr_data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert QR to base64
    qr_buffer = io.BytesIO()
    qr_img.save(qr_buffer, format='PNG')
    qr_buffer.seek(0)
    qr_base64 = f"data:image/png;base64,{base64.b64encode(qr_buffer.getvalue()).decode('utf-8')}"

    # Get student dates safely
    if hasattr(student, 'date_created') and student.date_created:
        date_issue = student.date_created.strftime('%b %Y')
        date_expiry = (student.date_created + timedelta(days=365)).strftime('%b %Y')
    else:
        date_issue = 'JAN 2025'
        date_expiry = 'JAN 2026'
    
    student_profile = StudentProfile.query.filter_by(user_id=student.user_id).first()
    index_number = student_profile.index_number if student_profile else "N/A"
    
    # Get programme safely
    programme_name = getattr(student, 'current_programme', None) or 'General Studies'
    
    # =====================================================
    # HTML template with BASE64 images
    # =====================================================
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            
            @page {{
                size: 85.6mm 53.98mm;
                margin: 0;
                padding: 0;
            }}
            
            html, body {{
                width: 85.6mm;
                height: 53.98mm;
                margin: 0;
                padding: 0;
            }}
            
            body {{
                font-family: 'Arial', sans-serif;
            }}
            
            .card {{
                width: 85.6mm;
                height: 53.98mm;
                border-radius: 3.2mm;
                overflow: hidden;
                position: relative;
            }}
            
            /* ===== FRONT SIDE ===== */
            .front {{
                background: linear-gradient(135deg, #F0F5FF 0%, #E8F4F8 100%);
                border: 0.5px solid #999;
            }}
            
            .front-header {{
                background: linear-gradient(90deg, #1E78B4 0%, #2A8FC5 100%);
                color: white;
                height: 14mm;
                display: flex;
                align-items: center;
                padding: 1.5mm 2mm;
                gap: 1.5mm;
            }}
            
            .logo {{
                height: 10mm;
                width: 10mm;
                object-fit: contain;
                flex-shrink: 0;
                background: white;
                padding: 0.5mm;
                border-radius: 1mm;
            }}
            
            .header-text {{
                font-size: 6.5pt;
                line-height: 1.1;
                font-weight: 500;
            }}
            
            .header-text strong {{
                font-size: 7.5pt;
                display: block;
            }}
            
            .motto {{
                position: absolute;
                top: 9.5mm;
                left: 25mm;
                background: #FF6464;
                color: white;
                font-size: 6pt;
                padding: 0.8mm 1.2mm;
                border-radius: 1.5mm;
                font-weight: bold;
                letter-spacing: 0.3pt;
            }}
            
            .profile-pic {{
                position: absolute;
                top: 14.5mm;
                right: 2mm;
                width: 16mm;
                height: 20mm;
                object-fit: cover;
                border: 1px solid #333;
                border-radius: 1mm;
                background: white;
            }}
            
            .student-info {{
                position: absolute;
                top: 14.5mm;
                left: 2mm;
                width: 60mm;
                font-size: 5.8pt;
                line-height: 1.4;
                color: #000;
            }}
            
            .student-info p {{
                margin: 0.5mm 0;
            }}
            
            .student-info strong {{
                color: #C81E1E;
                font-weight: bold;
            }}
            
            .info-label {{
                display: inline-block;
                width: 35mm;
            }}
            
            .qr-code {{
                position: absolute;
                bottom: 2mm;
                left: 2mm;
                width: 14mm;
                height: 14mm;
                background: white;
                padding: 0.5mm;
                border: 0.5px solid #ccc;
                image-rendering: pixelated;
            }}
            
            .footer {{
                position: absolute;
                bottom: 0.5mm;
                right: 2mm;
                font-size: 5.5pt;
                color: #555;
                font-weight: bold;
                letter-spacing: 0.5pt;
            }}
            
            /* ===== BACK SIDE ===== */
            .back {{
                background: linear-gradient(135deg, #E8F4F8 0%, #D4E9F2 100%);
                border: 0.5px solid #999;
                padding: 2.5mm;
                display: flex;
                flex-direction: column;
            }}
            
            .back-header {{
                background: #1E78B4;
                color: white;
                text-align: center;
                padding: 1.5mm;
                font-size: 6pt;
                font-weight: bold;
                margin-bottom: 1.5mm;
                border-radius: 1mm;
            }}
            
            .back-section {{
                margin-bottom: 1.5mm;
                font-size: 5.2pt;
                line-height: 1.3;
            }}
            
            .back-section h4 {{
                margin: 0 0 0.8mm 0;
                font-size: 5.8pt;
                color: #C81E1E;
                font-weight: bold;
                border-bottom: 0.5px solid #1E78B4;
                padding-bottom: 0.3mm;
            }}
            
            .back-section p {{
                margin: 0.3mm 0;
                color: #333;
            }}
            
            .back-section ul {{
                margin: 0.3mm 0 0 1.5mm;
                padding: 0;
            }}
            
            .back-section li {{
                margin: 0.2mm 0;
                list-style-type: disc;
            }}
            
            .signature-line {{
                border-top: 0.5px solid #000;
                margin-top: 1mm;
                padding-top: 0.5mm;
                font-size: 4.8pt;
                text-align: center;
                color: #666;
            }}
            
            .back-qr {{
                position: absolute;
                bottom: 2mm;
                right: 2mm;
                width: 12mm;
                height: 12mm;
                image-rendering: pixelated;
            }}
        </style>
    </head>
    <body>
        <!-- FRONT SIDE -->
        <div class="card front">
            <div class="front-header">
                {f'<img src="{logo_base64}" class="logo">' if logo_base64 else ''}
                <div class="header-text">
                    <strong>VTIU</strong>
                    VOCATIONAL & TECHNICAL INSPIRED UNIVERSITY
                </div>
            </div>
            
            <div class="motto">PATASI - KUAMSI</div>
            
            <img src="{profile_pic_base64}" class="profile-pic" alt="Student Photo">
            
            <div class="student-info">
                <p><span class="info-label"><strong>Name:</strong></span> {student.full_name}</p>
                <p><span class="info-label"><strong>Programme:</strong></span> {programme_name}</p>
                <p><span class="info-label"><strong>Index No:</strong></span> {index_number}</p>
                <p><span class="info-label"><strong>Issue:</strong></span> {date_issue}</p>
                <p><span class="info-label"><strong>Expiry:</strong></span> {date_expiry}</p>
            </div>
            
            <img src="{qr_base64}" class="qr-code" alt="QR Code">
            
            <div class="footer">STUDENT ID CARD</div>
        </div>

        <!-- PAGE BREAK for back side -->
        <div style="page-break-after: always;"></div>

        <!-- BACK SIDE -->
        <div class="card back">
            <div class="back-header">VOCATIONAL & TECHNICAL INSPIRED UNIVERSITY</div>
            
            <div class="back-section">
                <h4>Emergency Contact</h4>
                <p><strong>Tel:</strong> +233 123 456 789</p>
                <p><strong>Email:</strong> emergency@vtiu.edu.gh</p>
            </div>
            
            <div class="back-section">
                <h4>Rules & Guidelines</h4>
                <ul>
                    <li>Always carry this ID card on campus.</li>
                    <li>Report lost cards immediately.</li>
                    <li>Use for library & lab access only.</li>
                    <li>Valid for one academic year.</li>
                </ul>
            </div>
            
            <div class="back-section">
                <h4>Important Notice</h4>
                <p>This card is property of VTIU College. Unauthorized use is prohibited. Card must be surrendered upon request.</p>
            </div>
            
            <div class="signature-line">AUTHORIZED SIGNATURE</div>
            
            <img src="{qr_base64}" class="back-qr" alt="QR Code">
        </div>
    </body>
    </html>
    """

    # =====================================================
    # Generate Image
    # =====================================================
    try:
        # Update filename to PNG
        filename = f"id_card_{student.user_id}.png"
        file_path = os.path.join(upload_dir, filename)
        
        # Generate image using our image generator
        img_buf = generate_image_from_html(html_content, format="png", width=350)
        
        # Save the image
        with open(file_path, 'wb') as f:
            f.write(img_buf.getvalue())
        
        print(f"ID card generated successfully: {filename}")
        print(f"  Profile picture used: {profile_pic_filename}")
    except Exception as e:
        print(f"HTML image generation unavailable, creating PDF fallback: {e}")
        filename = f"id_card_{student.user_id}.pdf"
        file_path = os.path.join(upload_dir, filename)
        _generate_fallback_pdf(
            student, file_path, profile_pic_path, logo_path, index_number,
            programme_name, date_issue, date_expiry, qr_base64
        )
        return url_for('static', filename=f'uploads/id_cards/{filename}')

    return url_for('static', filename=f'uploads/id_cards/{filename}')

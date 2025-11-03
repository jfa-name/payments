# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE

from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request
from frappe.model.document import Document
from frappe.utils import call_hook_method, cint, flt, get_url

from payments.utils import create_payment_gateway


class StripeSettings(Document):
    supported_currencies = [
        "AED",
        "ALL",
        "ANG",
        "ARS",
        "AUD",
        "AWG",
        "BBD",
        "BDT",
        "BIF",
        "BMD",
        "BND",
        "BOB",
        "BRL",
        "BSD",
        "BWP",
        "BZD",
        "CAD",
        "CHF",
        "CLP",
        "CNY",
        "COP",
        "CRC",
        "CVE",
        "CZK",
        "DJF",
        "DKK",
        "DOP",
        "DZD",
        "EGP",
        "ETB",
        "EUR",
        "FJD",
        "FKP",
        "GBP",
        "GIP",
        "GMD",
        "GNF",
        "GTQ",
        "GYD",
        "HKD",
        "HNL",
        "HRK",
        "HTG",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "ISK",
        "JMD",
        "JPY",
        "KES",
        "KHR",
        "KMF",
        "KRW",
        "KYD",
        "KZT",
        "LAK",
        "LBP",
        "LKR",
        "LRD",
        "MAD",
        "MDL",
        "MNT",
        "MOP",
        "MRO",
        "MUR",
        "MVR",
        "MWK",
        "MXN",
        "MYR",
        "NAD",
        "NGN",
        "NIO",
        "NOK",
        "NPR",
        "NZD",
        "PAB",
        "PEN",
        "PGK",
        "PHP",
        "PKR",
        "PLN",
        "PYG",
        "QAR",
        "RUB",
        "SAR",
        "SBD",
        "SCR",
        "SEK",
        "SGD",
        "SHP",
        "SLL",
        "SOS",
        "STD",
        "SVC",
        "SZL",
        "THB",
        "TOP",
        "TTD",
        "TWD",
        "TZS",
        "UAH",
        "UGX",
        "USD",
        "UYU",
        "UZS",
        "VND",
        "VUV",
        "WST",
        "XAF",
        "XOF",
        "XPF",
        "YER",
        "ZAR",
    ]

    currency_wise_minimum_charge_amount = {
        "JPY": 50,
        "MXN": 10,
        "DKK": 2.50,
        "HKD": 4.00,
        "NOK": 3.00,
        "SEK": 3.00,
        "USD": 0.50,
        "AUD": 0.50,
        "BRL": 0.50,
        "CAD": 0.50,
        "CHF": 0.50,
        "EUR": 0.50,
        "GBP": 0.30,
        "NZD": 0.50,
        "SGD": 0.50,
    }

    def on_update(self):
        create_payment_gateway(
            "Stripe-" + self.gateway_name,
            settings="Stripe Settings",
            controller=self.gateway_name,
        )
        call_hook_method("payment_gateway_enabled", gateway="Stripe-" + self.gateway_name)
        if not self.flags.ignore_mandatory:
            self.validate_stripe_credentails()

    def validate_stripe_credentails(self):
        if self.publishable_key and self.secret_key:
            header = {
                "Authorization": "Bearer {}".format(
                    self.get_password(fieldname="secret_key", raise_exception=False)
                )
            }
            try:
                make_get_request(url="https://api.stripe.com/v1/charges", headers=header)
            except Exception:
                frappe.throw(_("Seems Publishable Key or Secret Key is wrong !!!"))

    def validate_transaction_currency(self, currency):
        if currency not in self.supported_currencies:
            frappe.throw(
                _(
                    "Please select another payment method. Stripe does not support transactions in currency '{0}'"
                ).format(currency)
            )

    def validate_minimum_transaction_amount(self, currency, amount):
        if currency in self.currency_wise_minimum_charge_amount:
            if flt(amount) < self.currency_wise_minimum_charge_amount.get(currency, 0.0):
                frappe.throw(
                    _("For currency {0}, the minimum transaction amount should be {1}").format(
                        currency, self.currency_wise_minimum_charge_amount.get(currency, 0.0)
                    )
                )

    def get_payment_url(self, **kwargs):
        return get_url(f"./stripe_checkout?{urlencode(kwargs)}")

    def create_request(self, data):
        import stripe

        self.data = frappe._dict(data)
        stripe.api_key = self.get_password(fieldname="secret_key", raise_exception=False)
        stripe.default_http_client = stripe.http_client.RequestsClient()

        try:
            self.integration_request = create_request_log(self.data, service_name="Stripe")
            return self.create_charge_on_stripe()

        except Exception as e:
            # short title for Error Log (max length safe)
            short_message = "Stripe create_request error: {0}".format(str(e))[:140]
            traceback_snippet = frappe.get_traceback()[:2000]
            frappe.log_error(traceback_snippet, short_message)
            return {
                "redirect_to": frappe.redirect_to_message(
                    _("Server Error"),
                    _(
                        "It seems that there is an issue with the server's stripe configuration. In case of failure, the amount will get refunded to your account."
                    ),
                ),
                "status": 401,
            }

    def create_charge_on_stripe(self):
        import stripe

        try:
            charge = stripe.Charge.create(
                amount=cint(flt(self.data.amount) * 100),
                currency=self.data.currency,
                source=self.data.stripe_token_id,
                description=self.data.description,
                receipt_email=self.data.payer_email,
            )

            if getattr(charge, "captured", False):
                self.integration_request.db_set("status", "Completed", update_modified=False)
                self.flags.status_changed_to = "Completed"
            else:
                # capture failed or not captured: log a short message
                msg = getattr(charge, "failure_message", "Charge not captured")
                frappe.log_error(msg[:140], "Stripe Payment not completed")

        except Exception as e:
            # try to detect stripe CardError specifically
            try:
                import stripe as _stripe

                if isinstance(e, _stripe.error.CardError):
                    err_text = str(e)
                    title = "Stripe CardError"
                    frappe.log_error(err_text[:140], title)
                    
                    # Marcar el Integration Request como Failed
                    self.integration_request.db_set("status", "Failed", update_modified=False)
                    
                    # Detectar si es error de 3DS
                    if "requires authentication" in err_text.lower() or "authentication required" in err_text.lower():
                        self.flags.payment_failed_reason = "3ds_required"
                        self.flags.status_changed_to = "Failed"
                        return self.finalize_request()
                    else:
                        self.flags.payment_failed_reason = "card_declined"
                        self.flags.status_changed_to = "Failed"
                        return self.finalize_request()
            except Exception:
                pass

            # Generic exception handler
            title = "Stripe create_charge_on_stripe error"
            traceback_snippet = frappe.get_traceback()[:2000]
            frappe.log_error(traceback_snippet, title[:140])
            self.integration_request.db_set("status", "Failed", update_modified=False)
            self.flags.payment_failed_reason = "generic_error"
            self.flags.status_changed_to = "Failed"
            return self.finalize_request()

        return self.finalize_request()

    def finalize_request(self):
        redirect_to = self.data.get("redirect_to") or None
        redirect_message = self.data.get("redirect_message") or None
        status = self.integration_request.status

        if getattr(self.flags, "status_changed_to", None) == "Completed":
            if self.data.reference_doctype and self.data.reference_docname:
                custom_redirect_to = None
                try:
                    custom_redirect_to = frappe.get_doc(
                        self.data.reference_doctype, self.data.reference_docname
                    ).run_method("on_payment_authorized", self.flags.status_changed_to)
                except Exception as e:
                    title = "on_payment_authorized hook error"
                    traceback_snippet = frappe.get_traceback()[:2000]
                    frappe.log_error(traceback_snippet, title[:140])
                    frappe.throw(
                        _(
                            "Ha ocurrido un error procesando la confirmación del pago. "
                            "Por favor, contacta con soporte."
                        )
                    )

                if custom_redirect_to:
                    redirect_to = custom_redirect_to

                redirect_url = "payment-success?doctype={}&docname={}".format(
                    self.data.reference_doctype, self.data.reference_docname
                )

            if self.redirect_url:
                redirect_url = self.redirect_url
                redirect_to = None
        else:
            # Payment failed - construct redirect with context
            redirect_params = {
                "doctype": self.data.get("reference_doctype", ""),
                "docname": self.data.get("reference_docname", ""),
                "reason": getattr(self.flags, "payment_failed_reason", "unknown"),
                "amount": self.data.get("amount", ""),
                "currency": self.data.get("currency", "")
            }
            redirect_url = "payment-failed?{}".format(urlencode(redirect_params))

        if redirect_to:
            redirect_url += ("&" if "?" in redirect_url else "?") + urlencode({"redirect_to": redirect_to})
        if redirect_message:
            redirect_url += "&" + urlencode({"redirect_message": redirect_message})

        return {"redirect_to": redirect_url, "status": status}


@frappe.whitelist(allow_guest=True)
def request_bank_transfer(doctype, docname):
    """
    Registra una solicitud de transferencia bancaria y envía email con instrucciones
    """
    if not doctype or not docname:
        frappe.throw(_("Missing document information"))
    
    # Verificar que el documento existe
    try:
        doc = frappe.get_doc(doctype, docname)
    except Exception:
        frappe.throw(_("Document not found"))
    
    # Verificar permisos (si es guest, verificar que tenga acceso al documento)
    if frappe.session.user == "Guest":
        # Para documentos públicos como Sales Order de carrito web
        if not doc.has_website_permission(frappe.session.user):
            frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    # Obtener configuración de transferencia bancaria
    bank_settings = frappe.get_single("Bank Transfer Settings")
    
    if not bank_settings or not bank_settings.enabled:
        frappe.throw(_("Bank transfer payment method is not configured"))
    
    # Crear Integration Request para tracking
    integration_request = frappe.get_doc({
        "doctype": "Integration Request",
        "integration_type": "Remote",
        "integration_request_service": "Bank Transfer",
        "reference_doctype": doctype,
        "reference_docname": docname,
        "status": "Queued",
        "data": frappe.as_json({
            "amount": doc.grand_total if hasattr(doc, "grand_total") else 0,
            "currency": doc.currency if hasattr(doc, "currency") else "EUR",
            "customer": doc.customer if hasattr(doc, "customer") else "",
            "customer_email": doc.contact_email if hasattr(doc, "contact_email") else doc.email_id if hasattr(doc, "email_id") else ""
        })
    })
    integration_request.insert(ignore_permissions=True)
    
    # Enviar email con instrucciones
    try:
        send_bank_transfer_instructions(doc, bank_settings, integration_request.name)
        integration_request.db_set("status", "Completed", update_modified=False)
        
        # Marcar Sales Order como "On Hold" si es Sales Order
        if doctype == "Sales Order":
            try:
                # Usar SQL directo para actualizar, ignorando validaciones
                frappe.db.sql("""
                    UPDATE `tabSales Order`
                    SET status = 'On Hold'
                    WHERE name = %s
                """, (docname,))
                
                # Añadir comentario en el timeline
                frappe.get_doc({
                    "doctype": "Comment",
                    "comment_type": "Info",
                    "reference_doctype": doctype,
                    "reference_name": docname,
                    "content": _("Order placed On Hold - Awaiting bank transfer payment confirmation. Payment reference: {0}").format(
                        integration_request.name
                    )
                }).insert(ignore_permissions=True)
                
                frappe.logger().info(f"Sales Order {docname} marked as On Hold")
                
            except Exception as e:
                frappe.logger().error(f"Error updating Sales Order status: {str(e)}")
                frappe.log_error(frappe.get_traceback(), "Sales Order On Hold Update Failed")
        
        frappe.db.commit()
        
        return {
            "success": True,
            "message": _("Bank transfer instructions have been sent to your email"),
            "redirect_to": f"/bank-transfer-requested?doctype={doctype}&docname={docname}"
        }
        
    except Exception as e:
        integration_request.db_set("status", "Failed", update_modified=False)
        frappe.log_error(frappe.get_traceback(), "Bank Transfer Request Failed")
        frappe.throw(_("Failed to send email. Please contact support."))

def get_customer_email(doc):
    """
    Obtiene el email del cliente de forma simple y directa
    """
    recipient_email = None
    
    # 1. Payment Request - campo email_to
    if doc.doctype == "Payment Request" and hasattr(doc, "email_to") and doc.email_to:
        recipient_email = doc.email_to
        frappe.logger().info(f"Email found in Payment Request.email_to: {recipient_email}")
        return recipient_email
    
    # 2. Sales Order y otros - campo contact_email
    if hasattr(doc, "contact_email") and doc.contact_email:
        recipient_email = doc.contact_email
        frappe.logger().info(f"Email found in {doc.doctype}.contact_email: {recipient_email}")
        return recipient_email
    
    # 3. Cualquier doctype - campo email_id
    if hasattr(doc, "email_id") and doc.email_id:
        recipient_email = doc.email_id
        frappe.logger().info(f"Email found in {doc.doctype}.email_id: {recipient_email}")
        return recipient_email
    
    # 4. Fallback: owner del documento (usuario que lo creó)
    if hasattr(doc, "owner") and doc.owner and doc.owner != "Administrator":
        # Verificar que el owner sea un email válido
        if "@" in doc.owner:
            recipient_email = doc.owner
            frappe.logger().info(f"Email found in {doc.doctype}.owner: {recipient_email}")
            return recipient_email
    
    # 5. Último recurso: buscar en Contact vinculado (solo si realmente existe contact_person)
    if hasattr(doc, "contact_person") and doc.contact_person:
        try:
            contact = frappe.get_doc("Contact", doc.contact_person)
            if contact.email_id:
                recipient_email = contact.email_id
                frappe.logger().info(f"Email found in Contact.email_id: {recipient_email}")
                return recipient_email
            # Buscar en email_ids child table
            if hasattr(contact, "email_ids") and contact.email_ids:
                for email_row in contact.email_ids:
                    if email_row.email_id:
                        recipient_email = email_row.email_id
                        frappe.logger().info(f"Email found in Contact.email_ids: {recipient_email}")
                        return recipient_email
        except Exception as e:
            frappe.logger().error(f"Error fetching contact: {str(e)}")
    
    frappe.logger().warning(f"No email found for document {doc.doctype} - {doc.name}")
    return recipient_email

def send_bank_transfer_instructions(doc, bank_settings, integration_request_name):
    """
    Envía email con instrucciones de transferencia bancaria
    """
    recipient_email = get_customer_email(doc)
    
    if not recipient_email:
        frappe.throw(_("Customer email not found. Please ensure the customer has a valid email address in their contact details."))
    
    # Preparar contexto para el template
    context = {
        "doc": doc,
        "bank_name": bank_settings.bank_name,
        "account_holder": bank_settings.account_holder,
        "iban": bank_settings.iban,
        "bic_swift": bank_settings.bic_swift,
        "amount": doc.grand_total if hasattr(doc, "grand_total") else 0,
        "currency": doc.currency if hasattr(doc, "currency") else "EUR",
        "reference": f"{doc.name}",
        "payment_reference": integration_request_name,
        "reply_to_email": bank_settings.reply_to_email or frappe.get_value("Email Account", {"default_outgoing": 1}, "email_id"),
        "additional_instructions": bank_settings.additional_instructions or ""
    }
    
    # Enviar email
    frappe.sendmail(
        recipients=[recipient_email],
        subject=_("Bank Transfer Instructions - Order {0}").format(doc.name),
        template="bank_transfer_instructions",
        args=context,
        reference_doctype=doc.doctype,
        reference_name=doc.name,
        reply_to=context["reply_to_email"]
    )

def get_gateway_controller(doctype, docname):
    reference_doc = frappe.get_doc(doctype, docname)
    return frappe.db.get_value(
        "Payment Gateway", reference_doc.payment_gateway, "gateway_controller"
    )

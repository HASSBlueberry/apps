"""Read new MyQ verification mail through an explicitly configured TLS mailbox."""
import email
from email import policy
from email.utils import getaddresses
import imaplib
import re
import ssl
import time


class MailboxError(ValueError):
    """Stable errors only. Mail and credentials must never reach logs."""


def verification_code(raw, recipient):
    message=email.message_from_bytes(raw,policy=policy.default)
    senders={address.lower() for _,address in getaddresses(message.get_all('From',[]))}
    recipients={address.lower() for _,address in getaddresses(message.get_all('To',[])+message.get_all('Delivered-To',[]))}
    if senders!={'notification@myq.com'} or recipient.lower() not in recipients:return None
    match=re.fullmatch(r'myQ Verification Code:\s*([A-Z0-9]{6})',str(message.get('Subject','')).strip(),re.I)
    return match[1].upper() if match else None


class VerificationMailbox:
    def __init__(self, options):
        self.options=options
        self.seen=set()

    def read(self, started_at, baseline=False):
        """Use server INTERNALDATE and read-only UID fetch, never mark mail read."""
        settings=self.options
        if not settings.get('imap_enabled'):raise MailboxError('imap_not_configured')
        try:
            with imaplib.IMAP4_SSL(settings['imap_host'],settings.get('imap_port',993),
                                  ssl_context=ssl.create_default_context(),timeout=15) as client:
                client.login(settings['imap_username'],settings['imap_password'])
                status,_=client.select(settings.get('imap_folder','INBOX'),readonly=True)
                if status!='OK':raise MailboxError('imap_folder_unavailable')
                status,data=client.uid('search',None,'FROM','"notification@myq.com"')
                if status!='OK':raise MailboxError('imap_search_failed')
                ids=data[0].split()[-50:]
                if baseline:
                    self.seen.update(ids)
                    return None
                deadline=time.monotonic()+20
                for uid in reversed(ids):
                    if time.monotonic()>deadline:break
                    if uid in self.seen:continue
                    status,rows=client.uid('fetch',uid,'(INTERNALDATE RFC822.SIZE BODY.PEEK[])')
                    if status!='OK':continue
                    for row in rows:
                        if not isinstance(row,tuple):continue
                        date=imaplib.Internaldate2tuple(row[0])
                        if date is None or time.mktime(date)<started_at-1:continue
                        if len(row[1])>256*1024:continue
                        code=verification_code(row[1],settings.get('myq_email',''))
                        if code:
                            self.seen.add(uid)
                            return code
                return None
        except MailboxError:raise
        except (OSError,imaplib.IMAP4.error,KeyError,ValueError):
            raise MailboxError('imap_connection_failed') from None

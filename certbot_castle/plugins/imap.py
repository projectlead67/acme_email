import logging, time, datetime

logging.basicConfig(
    format='%(asctime)s - %(levelname)s: %(message)s',
    level=logging.DEBUG
)

import zope.interface

from acme import messages
from certbot import interfaces
from certbot.plugins import common

from certbot_castle import challenge

import josepy as jose
from imapclient import IMAPClient
import imapclient
from smtplib import SMTP, SMTP_SSL
import ssl

logger = logging.getLogger(__name__)

@zope.interface.implementer(interfaces.IAuthenticator)
@zope.interface.provider(interfaces.IPluginFactory)
class Authenticator(common.Plugin):

    description = "Automatic S/MIME challenge by using IMAP integration"

    def __init__(self, *args, **kwargs):
        super(Authenticator, self).__init__(*args, **kwargs)

    @classmethod
    def add_parser_arguments(cls, add):
        add('login',help='IMAP login')
        add('password',help='IMAP password')
        add('host',help='IMAP server host')
        add('port',help='IMAP server port')
        add('ssl',help='IMAP SSL',action='store_true')
        
        add('smtp-method',help='SMTP method {STARTTLS,SSL,plain}',choices= ['STARTTLS','SSL','plain'])
        add('smtp-login',help='IMAP login')
        add('smtp-password',help='IMAP password')
        add('smtp-host',help='IMAP server host')
        add('smtp-port',help='IMAP server port')

    def more_info(self):  # pylint: disable=missing-function-docstring
        return("This authenticator performs an interactive email-reply-00 challenge. "
               "It configures an IMAP and SMTP e-mail clients to receive and answer ACME challenges. ")

    def prepare(self):  # pylint: disable=missing-function-docstring
        self.imap = IMAPClient(self.conf('host'), port=self.conf('port'), use_uid=False, ssl=True if self.conf('ssl') else False)
        self.imap.login(self.conf('login'),self.conf('password'))
        self.imap.select_folder('INBOX')
        self.imap.idle()
        
        method = self.conf('smtp-method')
        smtp_server = self.conf('smtp-host') if self.conf('smtp-host') else self.conf('host')
        port = self.conf('smtp-port') if self.conf('smtp-port') else self.conf('port')
        login = self.conf('smtp-login') if self.conf('smtp-login') else self.conf('login')
        password = self.conf('smtp-password') if self.conf('smtp-password') else self.conf('password')
        if (method == 'STARTTLS'):
            context = ssl.create_default_context()
            port = port if port else 587
            self.smtp = SMTP(self.conf('smtp-host'),port=port)
            self.smtp.ehlo()
            self.smtp.starttls(context=context) # Secure the connection
            self.smtp.ehlo() # Can be omitted
        elif (method == 'SSL'):
            context = ssl.create_default_context()
            port = port if port else 465
            self.smtp = SMTP_SSL(smtp_server,port=port,context=context)
        else:
            port = port if port else 25
            self.smtp = SMTP(smtp_server,port=port)
        self.smtp.login(login,password)

    def get_chall_pref(self, domain):
        # pylint: disable=unused-argument,missing-function-docstring
        return [challenge.EmailReply00]

    def perform(self, achalls):  # pylint: disable=missing-function-docstring
        return [self._perform_emailreply00(achall) for achall in achalls]

    def _perform_emailreply00(self, achall):
        response, _ = achall.challb.response_and_validation(achall.account_key)
        
        notify = zope.component.getUtility(interfaces.IDisplay).notification

        text = 'A challenge request for S/MIME certificate has been sent. In few minutes, ACME server will send a challenge e-mail to requested recipient. You do not need to take ANY action, as it will be replied automatically.'
        notify(text,pause=False)
        sent = False
        for i in range(30):
            idle = self.imap.idle_check(timeout=10)
            for msg in idle:
                uid, state = msg
                if state == b'EXISTS':
                    self.imap.idle_done()
                    respo = self.imap.fetch(uid, 'ENVELOPE')
                    for message_id, data in respo.items():
                        if (b'ENVELOPE' in data):
                            subject = data[b'ENVELOPE'].subject
                            if (subject.startswith(b'ACME: ')):
                                token64 = subject.split(b' ')[-1]
                                token1 = jose.b64.b64decode(token64)

                                full_token = bytearray(achall.chall.token)
                                full_token = token1+achall.chall.token

                                # We reconstruct the ChallengeBody
                                challt = messages.ChallengeBody.from_json({ 'type': 'email-reply-00', 'token': jose.b64.b64encode(bytes(full_token)).decode('ascii'), 'url': achall.challb.uri, 'status': achall.challb.status.to_json() })
                                response, validation = challt.response_and_validation(achall.account_key)
                                if (data[b'ENVELOPE'].reply_to):
                                    frm = data[b'ENVELOPE'].reply_to[0]
                                else:
                                    frm = data[b'ENVELOPE'].sender[0]
                                to = (frm.mailbox+b'@'+frm.host).decode('utf-8')
                                me = (data[b'ENVELOPE'].to[0].mailbox+b'@'+data[b'ENVELOPE'].to[0].host).decode('utf-8')
                                message = 'From: {}\n'.format(me)
                                message += 'To: {}\n'.format(to)
                                message += 'Subject: Re: {}\n\n'.format(subject.decode('utf-8'))
                                message += '-----BEGIN ACME RESPONSE-----\n{}\n-----END ACME RESPONSE-----\n'.format(validation)
                                self.smtp.sendmail(me,to,message)
                                
                                self.imap.add_flags(message_id,imapclient.SEEN)
                                self.imap.add_flags(message_id,imapclient.DELETED)
                                notify('The ACME response has been sent successfully!',pause=False)
                                sent = True
            if (sent):
                break
        return response

    def cleanup(self, achalls):  # pylint: disable=missing-function-docstring
        #self.imap.idle_done()
        self.imap.logout()
        self.smtp.quit()

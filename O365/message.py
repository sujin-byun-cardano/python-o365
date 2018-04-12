import logging
import base64
from dateutil.parser import parse
from tzlocal import get_localzone
import pytz
from pathlib import Path
from bs4 import BeautifulSoup as bs

from O365.utils import WellKnowFolderNames, ApiComponent, Attachments, Attachment, AttachableMixin

log = logging.getLogger(__name__)


class Recipient:
    """ A single Recipient """

    def __init__(self, address=None, name=None):
            self.address = address or ''
            self.name = name or ''

    def __bool__(self):
        return bool(self.address)

    def __str__(self):
        if self.name:
            return '{} ({})'.format(self.name, self.address)
        else:
            return self.address

    def __repr__(self):
        return self.__str__()


class Recipients:
    """ A Sequence of Recipients """

    def __init__(self, recipients=None):
        """ Recipients must be a list of either address strings or tuples (name, address) or dictionary elements """
        self.recipients = []
        if recipients:
            self.add(recipients)

    def __iter__(self):
        return iter(self.recipients)

    def __getitem__(self, key):
        return self.recipients[key]

    def __contains__(self, item):
        return item in {recipient.address for recipient in self.recipients}

    def __bool__(self):
        return bool(len(self.recipients))

    def __len__(self):
        return len(self.recipients)

    def __str__(self):
        return 'Recipients count: {}'.format(len(self.recipients))

    def __repr__(self):
        return self.__str__()

    def clear(self):
        self.recipients = []

    def add(self, recipients):
        """ Recipients must be a list of either address strings or tuples (name, address) or dictionary elements """

        if recipients:
            if isinstance(recipients, str):
                self.recipients.append(Recipient(address=recipients))
            elif isinstance(recipients, Recipient):
                self.recipients.append(recipients)
            elif isinstance(recipients, tuple):
                name, address = recipients
                if address:
                    self.recipients.append(Recipient(address=address, name=name))
            elif isinstance(recipients, list):
                for recipient in recipients:
                    self.add(recipient)
            else:
                raise ValueError('Recipients must be an address string, a'
                                 ' Recipient instance, a (name, address) tuple or a list')

    def remove(self, address):
        """ Remove an address or multiple addreses """
        recipients = []
        if isinstance(address, str):
            address = {address}  # set
        for recipient in self.recipients:
            if recipient.address not in address:
                recipients.append(recipient)
        self.recipients = recipients

    def get_first_recipient_with_address(self):
        """ Returns the first recipient found with a non blank address"""
        recipients_with_address = [recipient for recipient in self.recipients if recipient.address]
        if recipients_with_address:
            return recipients_with_address[0]
        else:
            return None


class MessageAttachment(Attachment):

    _endpoints = {'attach': '/messages/{id}/attachments'}


class MessageAttachments(Attachments):

    _endpoints = {'attachments': '/messages/{id}/attachments'}
    _attachment_constructor = MessageAttachment


class HandleRecipientsMixin:

    def _recipients_from_cloud(self, recipients):
        """ Transform a recipient from cloud data to object data """
        recipients_data = []
        for recipient in recipients:
            recipients_data.append(self._recipient_from_cloud(recipient))
        return Recipients(recipients_data)

    def _recipient_from_cloud(self, recipient):
        """ Transform a recipient from cloud data to object data """

        if recipient:
            recipient = recipient.get(self._cc('emailAddress'), recipient if isinstance(recipient, dict) else {})
            address = recipient.get(self._cc('address'), '')
            name = recipient.get(self._cc('name'), '')
            return Recipient(address=address, name=name)
        else:
            return Recipient()

    def _recipient_to_cloud(self, recipient):
        """ Transforms a Recipient object to a cloud dict """
        data = None
        if recipient:
            data = {self._cc('emailAddress'): {self._cc('address'): recipient.address}}
            if recipient.name:
                data[self._cc('emailAddress')][self._cc('name')] = recipient.name
        return data


class Message(ApiComponent, AttachableMixin, HandleRecipientsMixin):
    """ Management of the process of sending, receiving, reading, and editing emails. """

    _endpoints = {
        'create_draft': '/messages',
        'create_draft_folder': '/mailFolders/{id}/messages',
        'send_mail': '/sendMail',
        'send_draft': '/messages/{id}/send',
        'get_message': '/messages/{id}',
        'move_message': '/messages/{id}/move',
        'copy_message': '/messages/{id}/copy',
        'create_reply': '/messages/{id}/createReply',
        'create_reply_all': '/messages/{id}/createReplyAll',
        'forward_message': '/messages/{id}/createForward'
    }

    _importance_options = {'normal': 'normal', 'low': 'low', 'high': 'high'}

    def __init__(self, *, parent=None, con=None, **kwargs):
        """
        Makes a new message wrapper for sending and receiving messages.

        :param parent: the parent object
        :param con: the id of this message if it exists
        """
        assert parent or con, 'Need a parent or a connection'
        self.con = parent.con if parent else con

        # Choose the main_resource passed in kwargs over the parent main_resource
        main_resource = kwargs.pop('main_resource', None) or getattr(parent, 'main_resource', None) if parent else None
        super().__init__(protocol=parent.protocol if parent else kwargs.get('protocol'), main_resource=main_resource,
                         attachment_name_property='subject', attachment_type='message_type')

        download_attachments = kwargs.get('download_attachments')

        cloud_data = kwargs.get(self._cloud_data_key, {})
        cc = self._cc  # alias to shorten the code

        self.object_id = cloud_data.get(cc('id'), None)
        self.created = cloud_data.get(cc('createdDateTime'), None)
        self.received = cloud_data.get(cc('receivedDateTime'), None)
        self.sent = cloud_data.get(cc('sentDateTime'), None)

        local_tz = get_localzone()
        self.created = parse(self.created).astimezone(local_tz) if self.created else None
        self.received = parse(self.received).astimezone(local_tz) if self.received else None
        self.sent = parse(self.sent).astimezone(local_tz) if self.sent else None

        self.__attachments = MessageAttachments(parent=self, attachments=[])
        self.has_attachments = cloud_data.get(cc('hasAttachments'), 0)
        if self.has_attachments and download_attachments:
            self.attachments.download_attachments()
        self.subject = cloud_data.get(cc('subject'), '')
        body = cloud_data.get(cc('body'), {})
        self.body = body.get(cc('content'), '')
        self.body_type = body.get(cc('contentType'), 'HTML')  # default to HTML for new messages
        self.__sender = self._recipient_from_cloud(cloud_data.get(cc('from'), None))
        self.__to = self._recipients_from_cloud(cloud_data.get(cc('toRecipients'), []))
        self.__cc = self._recipients_from_cloud(cloud_data.get(cc('ccRecipients'), []))
        self.__bcc = self._recipients_from_cloud(cloud_data.get(cc('bccRecipients'), []))
        self.__reply_to = self._recipients_from_cloud(cloud_data.get(cc('replyTo'), []))
        self.__categories = cloud_data.get(cc('categories'), [])
        self.importance = self._importance_options.get(cloud_data.get(cc('importance'), 'normal'), 'normal')  # only allow valid importance
        self.is_read = cloud_data.get(cc('isRead'), None)
        self.is_draft = cloud_data.get(cc('isDraft'), kwargs.get('is_draft', True))  # a message is a draft by default
        self.conversation_id = cloud_data.get(cc('conversationId'), None)
        self.folder_id = cloud_data.get(cc('parentFolderId'), None)

    @property
    def attachments(self):
        """ Just to avoid api misuse by assigning to 'attachments' """
        return self.__attachments

    @property
    def sender(self):
        """ sender is a property to force to be allways a Recipient class """
        return self.__sender

    @sender.setter
    def sender(self, value):
        """ sender is a property to force to be allways a Recipient class """
        if isinstance(value, Recipient):
            self.__sender = value
        elif isinstance(value, str):
            self.__sender.address = value
        else:
            raise ValueError('sender must be an address string or a Recipient object')

    @property
    def to(self):
        """ Just to avoid api misuse by assigning to 'to' """
        return self.__to

    @property
    def cc(self):
        """ Just to avoid api misuse by assigning to 'cc' """
        return self.__cc

    @property
    def bcc(self):
        """ Just to avoid api misuse by assigning to 'bcc' """
        return self.__bcc

    @property
    def reply_to(self):
        """ Just to avoid api misuse by assigning to 'reply_to' """
        return self.__reply_to

    @property
    def categories(self):
        return self.__categories

    @categories.setter
    def categories(self, value):
        if isinstance(value, list):
            self.__categories = value
        elif isinstance(value, str):
            self.__categories = [value]
        elif isinstance(value, tuple):
            self.__categories = list(value)
        else:
            raise ValueError('categories must be a list')

    def to_api_data(self):
        """ Returns a dict representation of this message prepared to be send to the cloud """

        cc = self._cc  # alias to shorten the code

        message = {
            cc('subject'): self.subject,
            cc('body'): {
                cc('contentType'): self.body_type,
                cc('content'): self.body},
            cc('toRecipients'): [self._recipient_to_cloud(recipient) for recipient in self.to],
            cc('ccRecipients'): [self._recipient_to_cloud(recipient) for recipient in self.cc],
            cc('bccRecipients'): [self._recipient_to_cloud(recipient) for recipient in self.bcc],
            cc('replyTo'): [self._recipient_to_cloud(recipient) for recipient in self.reply_to],
            cc('attachments'): self.attachments.to_api_data()
        }

        if self.object_id and not self.is_draft:
            # return the whole signature of this message

            message[cc('id')] = self.object_id
            message[cc('createdDateTime')] = self.created.astimezone(pytz.utc).isoformat()
            message[cc('receivedDateTime')] = self.received.astimezone(pytz.utc).isoformat()
            message[cc('sentDateTime')] = self.sent.astimezone(pytz.utc).isoformat()
            message[cc('hasAttachments')] = len(self.attachments) > 0
            message[cc('from')] = self._recipient_to_cloud(self.sender)
            message[cc('categories')] = self.categories
            message[cc('importance')] = self.importance
            message[cc('isRead')] = self.is_read
            message[cc('isDraft')] = self.is_draft
            message[cc('conversationId')] = self.conversation_id
            message[cc('parentFolderId')] = self.folder_id  # this property does not form part of the message itself
        else:
            if self.sender and self.sender.address:
                message[cc('from')] = self._recipient_to_cloud(self.sender)

        return message

    def send(self, save_to_sent_folder=True):
        """ Sends this message. """

        if self.object_id and not self.is_draft:
            return RuntimeError('Not possible to send a message that is not new or a draft. Use Reply or Forward instead.')

        if self.is_draft and self.object_id:
            url = self.build_url(self._endpoints.get('send_draft').format(id=self.object_id))
            data = None
        else:
            url = self.build_url(self._endpoints.get('send_mail'))
            data = {self._cc('message'): self.to_api_data()}
            if save_to_sent_folder is False:
                data[self._cc('saveToSentItems')] = False

        try:
            response = self.con.post(url, data=data)
        except Exception as e:
            log.error('Message could not be send. Error: {}'.format(str(e)))
            return False

        if response.status_code != 202:
            log.debug('Message failed to be sent. Reason: {}'.format(response.reason))
            return False

        self.object_id = 'sent_message' if not self.object_id else self.object_id
        self.is_draft = False

        return True

    def reply(self, to_all=True):
        """
        Creates a new message that is a reply to this message.
        :param to_all: replies to all the recipients instead to just the sender
        """
        if not self.object_id or self.is_draft:
            raise RuntimeError("Can't reply to this message")

        if to_all:
            url = self.build_url(self._endpoints.get('create_reply_all').format(id=self.object_id))
        else:
            url = self.build_url(self._endpoints.get('create_reply').format(id=self.object_id))

        try:
            response = self.con.post(url)
        except Exception as e:
            log.error('message (id: {}) could not be replied. Error: {}'.format(self.object_id, str(e)))
            return None

        if response.status_code != 201:
            log.debug('message (id: {}) could not be replied. Reason: {}'.format(self.object_id, response.reason))
            return None

        message = response.json()

        # Everything received from the cloud must be passed with self._cloud_data_key
        return self.__class__(parent=self, **{self._cloud_data_key: message})

    def forward(self):
        """
        Creates a new message that is a forward of this message.
        """
        if not self.object_id or self.is_draft:
            raise RuntimeError("Can't forward this message")

        url = self.build_url(self._endpoints.get('forward_message').format(id=self.object_id))

        try:
            response = self.con.post(url)
        except Exception as e:
            log.error('message (id: {}) could not be forward. Error: {}'.format(self.object_id, str(e)))
            return None

        if response.status_code != 201:
            log.debug('message (id: {}) could not be forward. Reason: {}'.format(self.object_id, response.reason))
            return None

        message = response.json()

        # Everything received from the cloud must be passed with self._cloud_data_key
        return self.__class__(parent=self, **{self._cloud_data_key: message})

    def delete(self):
        """ Deletes a stored message """
        if self.object_id is None:
            raise RuntimeError('Attempting to delete an unsaved Message')

        url = self.build_url(self._endpoints.get('get_message').format(id=self.object_id))

        try:
            response = self.con.delete(url)
        except Exception as e:
            log.error('Message (id: {}) could not be deleted. Error: {}'.format(self.object_id, str(e)))
            return False

        if response.status_code != 204:
            log.debug('Message (id: {}) could not be deleted. Reason: {}'.format(self.object_id, response.reason))
            return False

        return True

    def mark_as_read(self):
        """ Marks this message as read in the cloud."""
        if self.object_id is None or self.is_draft:
            raise RuntimeError('Attempting to mark as read an unsaved Message')

        data = {self._cc('isRead'): True}

        url = self.build_url(self._endpoints.get('get_message').format(id=self.object_id))
        try:
            response = self.con.patch(url, data=data)
        except Exception as e:
            log.error('Message (id: {}) could not be marked as read. Error: {}'.format(self.object_id, str(e)))
            return False

        if response.status_code != 200:
            log.debug('Message (id: {}) could not be marked as read. Reason: {}'.format(self.object_id, response.reason))
            return False

        self.is_read = True

        return True

    def move(self, folder):
        """
        Move the message to a given folder

        :param folder: Folder object or Folder id or Well-known name to move this message to
        :returns: True on success
        """
        if self.object_id is None:
            raise RuntimeError('Attempting to move an unsaved Message')

        url = self.build_url(self._endpoints.get('move_message').format(id=self.object_id))

        if isinstance(folder, str):
            folder_id = folder
        else:
            folder_id = getattr(folder, 'folder_id', None)

        if not folder_id:
            raise RuntimeError('Must Provide a valid folder_id')

        data = {self._cc('destinationId'): folder_id}
        try:
            response = self.con.post(url, data=data)
        except Exception as e:
            log.error('Message (id: {}) could not be moved to folder id: {}. Error: {}'.format(self.object_id, folder_id, str(e)))
            return False

        if response.status_code != 201:
            log.debug('Message (id: {}) could not be moved to folder id: {}. Reason: {}'.format(self.object_id, folder_id, response.reason))
            return False

        self.folder_id = folder_id

        return True

    def copy(self, folder):
        """
        Copy the message to a given folder

        :param folder: Folder object or Folder id or Well-known name to move this message to
        :returns: the copied message
        """
        if self.object_id is None:
            raise RuntimeError('Attempting to move an unsaved Message')

        url = self.build_url(self._endpoints.get('copy_message').format(id=self.object_id))

        if isinstance(folder, str):
            folder_id = folder
        else:
            folder_id = getattr(folder, 'folder_id', None)

        if not folder_id:
            raise RuntimeError('Must Provide a valid folder_id')

        data = {self._cc('destinationId'): folder_id}
        try:
            response = self.con.post(url, data=data)
        except Exception as e:
            log.error('Message (id: {}) could not be copied to folder id: {}. Error: {}'.format(self.object_id, folder_id, str(e)))
            return None

        if response.status_code != 201:
            log.debug('Message (id: {}) could not be copied to folder id: {}. Error: {}'.format(self.object_id, folder_id, response.reason))
            return None

        message = response.json()

        # Everything received from the cloud must be passed with self._cloud_data_key
        return self.__class__(parent=self, **{self._cloud_data_key: message})

    def update_category(self, categories):
        """ Update this message categories """
        if not isinstance(categories, (list, tuple)):
            raise ValueError('Categories must be a list or tuple')

        if self.object_id is None:
            raise RuntimeError('Attempting to update an unsaved Message')

        data = {self._cc('categories'): categories}

        url = self.build_url(self._endpoints.get('get_message').format(id=self.object_id))
        try:
            response = self.con.patch(url, data=data)
        except Exception as e:
            log.error('Categories not updated. Error: {}'.format(str(e)))
            return False

        if response.status_code != 200:
            log.debug('Categories not updated. Reason: {}'.format(response.reason))
            return False

        self.categories = response.json().get(self._cc('categories'), [])
        return True

    def save_draft(self, target_folder=WellKnowFolderNames.DRAFTS):
        """ Save this message as a draft on the cloud """

        if not self.is_draft:
            raise RuntimeError('Only draft messages can be saved as drafts')
        if self.object_id:
            raise RuntimeError('This message has been already saved to the cloud')

        data = self.to_api_data()

        if not isinstance(target_folder, str):
            target_folder = getattr(target_folder, 'folder_id', None)

        if target_folder and target_folder is not WellKnowFolderNames.DRAFTS:
            url = self.build_url(self._endpoints.get('create_draft_folder').format(id=target_folder))
        else:
            url = self.build_url(self._endpoints.get('create_draft'))

        try:
            response = self.con.post(url, data=data)
        except Exception as e:
            log.error('Error saving draft. Error: {}'.format(str(e)))
            return False

        if response.status_code != 201:
            log.debug('Saving draft Request failed: {}'.format(response.reason))
            return False

        message = response.json()
        self.object_id = message.get(self._cc('id'), None)
        self.folder_id = message.get(self._cc('parentFolderId'), None)

        return True

    def get_body_text(self):
        """ Parse the body html and returns the body text using bs4 """
        if self.body_type != 'HTML':
            return self.body

        try:
            soup = bs(self.body, 'html.parser')
        except Exception as e:
            return self.body
        else:
            return soup.body.text

    def get_body_soup(self):
        """ Returns the beautifulsoup4 of the html body"""
        if self.body_type != 'HTML':
            return None
        else:
            return bs(self.body, 'html.parser')

    def __str__(self):
        return 'Subject: {}'.format(self.subject)

    def __repr__(self):
        return self.__str__()

from django.urls import reverse
from rest_framework import status

from core.models import SupportTicket, SupportMessage
from .test_base import CoreAPITestCase, User


class SupportWorkflowTests(CoreAPITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.doctor_user)
        
        # Create a support ticket
        self.ticket = SupportTicket.objects.create(
            user=self.doctor_user,
            subject="Test Ticket",
            priority=SupportTicket.PRIORITY_MEDIUM,
        )
        
        # Doctor's initial message
        self.doctor_message = SupportMessage.objects.create(
            ticket=self.ticket,
            user=self.doctor_user,
            message="This is a test message from the doctor."
        )
        
        # Admin's reply
        self.admin_message = SupportMessage.objects.create(
            ticket=self.ticket,
            user=self.admin_user,
            message="This is a reply from the admin."
        )

    def test_mark_read_action_updates_message_status(self):
        self.client.force_authenticate(user=self.doctor_user)
        
        # Verify initial status
        self.assertNotEqual(self.admin_message.status, SupportMessage.STATUS_READ)
        
        # Call the mark_read action
        url = reverse("supportticket-mark-read", args=[self.ticket.id])
        response = self.client.patch(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'success')
        self.assertEqual(response.data['updated_count'], 1)
        
        # Refresh messages from DB
        self.admin_message.refresh_from_db()
        self.doctor_message.refresh_from_db()
        
        # Assert that the admin's message is read
        self.assertEqual(self.admin_message.status, SupportMessage.STATUS_READ)
        
        # Assert that the doctor's own message is not marked as read by this action
        self.assertNotEqual(self.doctor_message.status, SupportMessage.STATUS_READ)

    def test_reply_to_ticket_creates_message_and_notifies(self):
        self.client.force_authenticate(user=self.admin_user)
        
        url = reverse("supportticket-reply", args=[self.ticket.id])
        reply_message = "This is another reply from the admin."
        
        response = self.client.post(url, {'message': reply_message})
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SupportMessage.objects.count(), 3)
        
        new_message = SupportMessage.objects.get(id=response.data['id'])
        self.assertEqual(new_message.message, reply_message)
        self.assertEqual(new_message.user, self.admin_user)

    def test_resolve_ticket_action(self):
        self.client.force_authenticate(user=self.admin_user)
        
        url = reverse("supportticket-resolve", args=[self.ticket.id])
        response = self.client.patch(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, SupportTicket.STATUS_RESOLVED)

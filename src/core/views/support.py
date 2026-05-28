from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.mail import send_mail
from django.conf import settings
from ..models import SupportTicket, SupportMessage, User
from ..serializers import SupportTicketSerializer, SupportMessageSerializer

class SupportTicketViewSet(viewsets.ModelViewSet):
    queryset = SupportTicket.objects.none()
    serializer_class = SupportTicketSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SupportTicket.objects.none()

        user = self.request.user
        if user.role == User.ROLE_ADMIN or user.is_staff:
            return SupportTicket.objects.all().order_by('-created_at')
        return SupportTicket.objects.filter(user=user).order_by('-created_at')

    def perform_create(self, serializer):
        ticket = serializer.save(user=self.request.user)
        # Create the initial message if passed in the request, or just save the ticket
        initial_message = self.request.data.get('message')
        if initial_message:
            SupportMessage.objects.create(ticket=ticket, user=self.request.user, message=initial_message)
        
        # Send email to admin
        admin_emails = User.objects.filter(role=User.ROLE_ADMIN).values_list('email', flat=True)
        if admin_emails:
            send_mail(
                subject=f"New Support Ticket: {ticket.subject}",
                message=f"Doctor {self.request.user.username} has opened a new support ticket.\n\nPriority: {ticket.priority}\nMessage: {initial_message or 'No initial message'}",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=list(admin_emails),
                fail_silently=True,
            )

    @action(detail=True, methods=['post'])
    def reply(self, request, pk=None):
        ticket = self.get_object()
        message_text = request.data.get('message')
        if not message_text:
            return Response({'error': 'Message text is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        message = SupportMessage.objects.create(
            ticket=ticket,
            user=request.user,
            message=message_text
        )

        # Notify the other party
        if request.user.role == User.ROLE_ADMIN or request.user.is_staff:
            # Notify doctor
            if ticket.user.email:
                send_mail(
                    subject=f"Update on Support Ticket: {ticket.subject}",
                    message=f"An admin has replied to your ticket.\n\nReply: {message_text}",
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[ticket.user.email],
                    fail_silently=True,
                )
        else:
            # Notify admin
            admin_emails = User.objects.filter(role=User.ROLE_ADMIN).values_list('email', flat=True)
            if admin_emails:
                send_mail(
                    subject=f"Update on Support Ticket: {ticket.subject}",
                    message=f"Doctor {request.user.username} has replied to the ticket.\n\nReply: {message_text}",
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=list(admin_emails),
                    fail_silently=True,
                )

        # Ensure ticket is updated
        ticket.updated_at = message.created_at
        ticket.save()

        serializer = SupportMessageSerializer(message)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch'])
    def resolve(self, request, pk=None):
        user = request.user
        if not (user.role == User.ROLE_ADMIN or user.is_staff):
            return Response({'error': 'Only admins can resolve tickets'}, status=status.HTTP_403_FORBIDDEN)
        
        ticket = self.get_object()
        ticket.status = SupportTicket.STATUS_RESOLVED
        ticket.save()

        if ticket.user.email:
            send_mail(
                subject=f"Support Ticket Resolved: {ticket.subject}",
                message=f"Your support ticket has been marked as resolved by an administrator.",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[ticket.user.email],
                fail_silently=True,
            )

        serializer = self.get_serializer(ticket)
        return Response(serializer.data)

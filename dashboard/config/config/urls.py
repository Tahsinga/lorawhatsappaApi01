"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path
from webui import views

urlpatterns = [
    path('control-room/', views.inbox, name='control_room'),
    path('', views.inbox, name='inbox'),
    path('api/messages/', views.messages_api, name='messages_api'),
    path('api/reply/', views.reply_api, name='reply_api'),
    path('media/messages/<str:filename>', views.message_media, name='message_media'),
    path('admin/', admin.site.urls),
]

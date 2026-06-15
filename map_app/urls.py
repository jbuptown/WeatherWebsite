from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('api/points/', views.get_points, name='get_points'),
    path('api/points/add/', views.add_point, name='add_point'),
    path('api/points/<int:point_id>/delete/', views.delete_point, name='delete_point'),
    path('api/predict/', views.predict_trajectory, name='predict_trajectory'),
]

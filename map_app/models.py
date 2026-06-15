from django.db import models


class MapPoint(models.Model):
    name = models.CharField(max_length=200, default='Без названия')
    description = models.TextField(blank=True, default='')
    latitude = models.FloatField()
    longitude = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.latitude:.4f}, {self.longitude:.4f})"

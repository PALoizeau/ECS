from channels.generic.websocket import WebsocketConsumer
from asgiref.sync import async_to_sync
import json

class updateConsumer(WebsocketConsumer):
    def connect(self):
        self.pcaId = self.scope['url_route']['kwargs']['pca_id']
        self.group_name = self.pcaId
        # Join pca/ecs group
        async_to_sync(self.channel_layer.group_add)(
            self.group_name,
            self.channel_name
        )

        self.accept()

    def disconnect(self, close_code):
        pass

    def receive(self, text_data):
        """receive message from websocket"""
        #currently not used (could possibly be used instead of ajax requests for commands)
        print (text_data)
        text_data_json = json.loads(text_data)
        message = text_data_json["message"]

    def update(self,event):
        message = {
            "type": "state",
            "message": event["text"]
        }
        if "origin" in event:
            message["origin"] = event["origin"]
        self.send(text_data=json.dumps(message))


    def logUpdate(self,event):
        message = {
            "type": "log",
            "message": event["text"]
        }
        if "origin" in event:
            message["origin"] = event["origin"]
        self.send(text_data=json.dumps(message))

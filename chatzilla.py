from gevent import monkey
from flask import Flask, Response, render_template, request
from socketio import socketio_manage
from socketio.namespace import BaseNamespace
from socketio.mixins import BroadcastMixin, RoomsMixin
from time import time
import os
import pymongo
from bson.objectid import ObjectId

monkey.patch_all()

application = Flask(__name__)
application.debug = True
application.config['PORT'] = 5000

CHAT_MESSAGE_COLLECTION = os.getenv('CHAT_MESSAGE_COLLECTION', 'chat_messages')
ROOM_COLLECTION = os.getenv('ROOM_COLLECTION', 'rooms')
USER_COLLECTION = os.getenv('USER_COLLECTION', 'users')

class ChatNamespace(BaseNamespace, BroadcastMixin, RoomsMixin):
    
    stats = {
        "people" : []
    }

    @classmethod
    def get_db_conn(cls):
        if getattr(cls, 'db', None):
            return cls.db

        # Retrieve database credentials from environment
        db_uri = os.getenv('DATABASE_URI', 'mongodb://localhost:27017/')
        db_user = os.getenv('DATABASE_USER', '')
        db_password = os.getenv('DATABASE_PASSWORD', '')
        db_name = os.getenv('DATABASE_NAME', '')

        # Initialize database client and database connection
        try:
            db_client = pymongo.MongoReplicaSetClient(db_uri, replicaSet='repl0')
        except:
            db_client = pymongo.MongoClient(db_uri)
        db = db_client[db_name]
        if db_user and db_password:
            db.authenticate(db_user, db_password)

        cls.db = db

        return cls.db

    def initialize(self):
        self.logger = application.logger
        self.log("Socketio session started")

    def log(self, message):
        self.logger.info("[{0}] {1}".format(self.socket.sessid, message))

    def report_stats(self):
        self.broadcast_event("stats",self.stats)

    def recv_connect(self):
        self.log("New connection")

    def recv_disconnect(self):
        self.log("Client disconnected")
        
        if self.session.has_key("user_id"):
            email = self.session['email']

            self.broadcast_event_not_me("debug", "%s left" % email)
            
            self.stats["people"] = filter(lambda e : e != email, self.stats["people"])
            self.report_stats()

    def on_join(self, user_id, email):
        self.log("%s joined chat" % email)
        self.session['user_id'] = user_id
        self.session['email'] = email

        if not email in self.stats["people"]:
            self.stats["people"].append(email) 

        self.report_stats()

        return True, user_id, email

    def on_subscribe(self, room):
        self.join(room)

        # Retrieve message history for this room
        messages = self.get_message_history(room)

        return True, messages

    def on_unsubscribe(self, room):
        self.leave(room)
        return True, room

    def on_message(self, message):
        room = message['room']
        message_content = message['content']
        client_sent = message['client_sent']

        message_data = {
            "sender_id" : self.session["user_id"],
            "sender" : self.session["email"],
            "room" : room,
            "content" : message_content,
            "client_sent" : client_sent,
            "sent" : time()*1000 #ms
        }

        # Record message to database
        message_data = self.record_message(message_data)

        # Emit message after inserting it into the database so that we can
        # include the ID of the database entry in the emitted message
        self.emit_to_room(room, "message", message_data)

        return True, message_data

    def on_remove_message(self, message_data):
        user_id = self.session['user_id']
        message_id = message_data['id']
        message_sender_id = message_data['sender_id']
        room = message_data['room']

        # Verify that the user has permission to remove this message
        if not self.has_permission_to_remove(user_id, room, message_sender_id):
            return False, {}

        self.emit_to_room(room, 'remove_message', message_id)

        self.record_removed_message(message_id)

        return True, message_data

    def on_avatar_url(self, user_id):
        return False, ''
        # Retrieve avatar URL from user collection, if available
        db = self.__class__.get_db_conn()
        user = db[USER_COLLECTION].find_one({'_id' : user_id })

        if not user:
            return False, ''

        avatar_url = user.get('avatar_url', '')
        
        if not avatar_url:
            return False, ''

        return True, avatar_url

    def record_message(self, message_data):
        # Create a copy since the insert command will mutate the dict
        message_data_copy = message_data.copy()

        # Insert into database
        db = self.__class__.get_db_conn()
        _id = db[CHAT_MESSAGE_COLLECTION].insert(message_data_copy, w=1)

        # Store the ID of the database entry
        message_data['id'] = str(_id)

        return message_data

    def has_permission_to_remove(self, user_id, room_id, message_sender_id):
        # Allow users to remove their own messages
        if user_id == message_sender_id:
            return True

        # Allow the owner of the room to remove any messages
        return self.is_owner_of_room(user_id, room_id)

    def is_owner_of_room(self, user_id, room_id):
        return False
        db = self.__class__.get_db_conn()
        room = db[ROOM_COLLECTION].find_one({'_id' : room_id}, {'username' : 1})
        room_owner = room.get('username')
        return user_id == room_owner

    def record_removed_message(self, message_id):
        db = self.__class__.get_db_conn()
        db[CHAT_MESSAGE_COLLECTION].update(
                { '_id' : ObjectId(message_id) },
                { '$set' : { 'removed' : True }},
                w=0
        )

    def get_message_history(self, room):
        db = self.__class__.get_db_conn()

        message_cursor = db[CHAT_MESSAGE_COLLECTION].find(
                            {'room' : room}
                         ).sort('sent', pymongo.ASCENDING)

        messages = []
        for message in message_cursor:
            message['id'] = str(message.pop('_id'))
            messages.append(message)

        return messages


@application.route('/', methods=['GET'])
def landing():
    return render_template('landing.html')

@application.route('/socket.io/<path:remaining>')
def socketio(remaining):
    try:
        socketio_manage(request.environ, {'/chat': ChatNamespace}, request)
    except:
        application.logger.error("Exception while handling socketio connection",
                         exc_info=True)
    return Response()
